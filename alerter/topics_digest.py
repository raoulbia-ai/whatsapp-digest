#!/usr/bin/env python3
"""Weekly curated AI digest from the Agentics community WhatsApp channels.

A SEPARATE pipeline from the kids' training alerts (listener.py / digest.py). Those track dated
events; this one curates *signal* — releases, tools, how-tos, useful links and substantive
discussion takeaways — out of two high-noise group chats ("Ai Code Chat", "General Chat") and
sends one digest to the operator's own chat.

The model only EXTRACTS structured items from each transcript chunk; CODE owns dedup (so the same
link/topic is never reported twice across weekly runs), grouping and rendering. A persistent
"seen" file keys every reported item by normalised URL (or title) so recurring digests only carry
what's new since last time.

Run manually:
    python3 topics_digest.py --days 30 --dry-run     # first backfill, print only
    python3 topics_digest.py                         # weekly run (lookback from config), sends
Scheduled via: wa-topics-digest.timer (weekly).
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CFG = json.load(open(os.path.expanduser("~/.config/wa-alerts/config.json")))
TD = CFG.get("topic_digest", {})
DB = os.environ.get("WA_MESSAGES_DB") or CFG.get("messages_db") or os.path.expanduser(
    "~/whatsapp-mcp/whatsapp-bridge/store/messages.db"
)
TZ = ZoneInfo("Europe/Dublin")
STATE_DIR = os.path.expanduser("~/.local/share/wa-alerts/state")
SEEN_PATH = os.path.join(STATE_DIR, TD.get("seen_file", "topics_seen.json"))

# How big each transcript chunk handed to the model may get (chars / messages). Keeping chunks
# focused makes the model both faster and less likely to drop items in a long scroll-back.
CHUNK_CHARS = 12000
CHUNK_MSGS = 140
URL_RE = re.compile(r"https?://[^\s)>\]]+")

# Render order, heading and emoji per category. The model must pick one of these keys.
CATEGORIES = [
    ("release", "🚀", "New & Notable"),
    ("tool", "🛠️", "Tools & Repos"),
    ("guide", "📚", "How-tos & Guides"),
    ("article", "🔗", "Links & Reads"),
    ("insight", "💡", "Insights & Discussion"),
    ("event", "📅", "Events & Talks"),
]
CAT_KEYS = {k for k, _, _ in CATEGORIES}


def token():
    t = (CFG.get("bridge_token") or "").strip()
    return t or open(CFG["bridge_token_path"]).read().strip()


def bridge_post(path, payload, timeout=60):
    req = urllib.request.Request(
        CFG["bridge_url"].rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_self(text):
    bridge_post("/send", {"recipient": CFG["self_jid"], "message": text}, timeout=30)


# ---- dedup state -----------------------------------------------------------------------------

def norm_url(url):
    """Canonicalise a URL for dedup: drop scheme/www, tracking params and trailing slash."""
    u = url.strip().rstrip(".,);]")
    u = re.sub(r"^https?://(www\.)?", "", u, flags=re.I)
    if "?" in u:
        base, q = u.split("?", 1)
        keep = [
            p for p in q.split("&")
            if p and not re.match(r"(utm_|fbclid|gclid|ref|ref_src|s=|t=|si=|igsh=)", p, re.I)
        ]
        u = base + ("?" + "&".join(keep) if keep else "")
    return u.rstrip("/").lower()


def item_key(item):
    """Stable dedup key: normalised URL if present, else a slug of the title."""
    url = (item.get("url") or "").strip()
    if url:
        return "u:" + norm_url(url)
    slug = re.sub(r"[^a-z0-9]+", "-", (item.get("title") or "").lower()).strip("-")
    return "t:" + slug[:60]


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_seen(seen):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, ensure_ascii=False, indent=0)
    os.replace(tmp, SEEN_PATH)


def prune_seen(seen, before_iso):
    return {k: v for k, v in seen.items() if v >= before_iso}


# ---- transcript ------------------------------------------------------------------------------

def fetch_messages(jid, days):
    c = sqlite3.connect(DB)
    rows = c.execute(
        "select datetime(timestamp), sender, content, media_type "
        "from messages where chat_jid=? and timestamp >= datetime('now', ?) "
        "and coalesce(media_type,'') not in ('reaction') order by timestamp",
        (jid, f"-{days} days"),
    ).fetchall()
    c.close()
    return rows


def transcript_lines(rows):
    """One readable line per message; images/audio noted briefly (we can't OCR these channels)."""
    out = []
    for ts, sender, content, mtype in rows:
        who = "+" + re.sub(r"\D", "", sender or "")[-4:]
        content = (content or "").strip()
        if mtype == "image":
            tag = f"[image]{(' ' + content) if content else ''}"
            out.append(f"[{ts}] {who}: {tag}")
        elif mtype in ("audio", "video", "document"):
            tag = f"[{mtype}]{(' ' + content) if content else ''}"
            out.append(f"[{ts}] {who}: {tag}")
        elif content:
            out.append(f"[{ts}] {who}: {content}")
    return out


def chunk(lines):
    batches, cur, size = [], [], 0
    for ln in lines:
        if cur and (size + len(ln) > CHUNK_CHARS or len(cur) >= CHUNK_MSGS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        batches.append(cur)
    return batches


# ---- extraction ------------------------------------------------------------------------------

EXTRACT_PROMPT = """You are curating a weekly digest of genuinely useful AI/engineering signal out
of a noisy WhatsApp group chat called "{channel}". Below is a chunk of its recent messages, each
prefixed with [YYYY-MM-DD HH:MM] and an anonymised sender.

Extract ONLY items a busy AI engineer would be glad to have saved a week later:
- New model / product / framework RELEASES and announcements        -> category "release"
- TOOLS, libraries, repos, services worth trying                    -> category "tool"
- HOW-TOS, techniques, tips, prompt/agent patterns, lessons learned -> category "guide"
- ARTICLES, papers, blog posts, threads, videos worth reading       -> category "article"
- Substantive INSIGHTS / conclusions from a discussion (even with no link) -> category "insight"
- EVENTS: meetups, webinars, masterclasses, talks (with date if stated)    -> category "event"

AGGRESSIVELY DROP noise: greetings, thanks, reactions, one-liners, "anyone tried X?" with no
answer, logistics, banter, opinions with no takeaway, job/self-promo spam, and anything not about
AI / software / engineering. When several messages discuss the SAME thing, MERGE them into ONE
item. Prefer items with a concrete artifact (link, repo, model, technique). If a message only
reacts to a link without adding info, fold it into that link's item. When in doubt, leave it out —
a short high-signal digest beats a long noisy one.

For each kept item output:
- "category": one of release|tool|guide|article|insight|event
- "title": a concise, self-contained headline (NOT "someone shared a link")
- "summary": 1-2 sentences on what it is and why it matters, understandable without the chat
- "url": the primary link if the messages contain one, else ""

Output ONLY a JSON array, nothing before or after it (no prose, no markdown fences). Each element:
{{"category":"...","title":"...","summary":"...","url":""}}
Return [] if this chunk has nothing worth keeping.

MESSAGES:
{body}"""


def extract_chunk(channel, body, timeout):
    prompt = EXTRACT_PROMPT.format(channel=channel, body=body)
    proc = subprocess.run(
        ["claude", "-p", prompt], capture_output=True, text=True, timeout=timeout, cwd="/tmp"
    )
    raw = (proc.stdout or "").strip()
    m = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    items = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        cat = (it.get("category") or "").lower().strip()
        if cat not in CAT_KEYS:
            cat = "insight" if not (it.get("url") or "").strip() else "article"
        title = (it.get("title") or "").strip()
        if not title:
            continue
        url = (it.get("url") or "").strip()
        m2 = URL_RE.search(url) or (URL_RE.search(it.get("summary", "")) if not url else None)
        items.append({
            "category": cat,
            "title": title,
            "summary": re.sub(r"\s+", " ", (it.get("summary") or "").strip()),
            "url": (m2.group(0) if m2 else url).rstrip(".,);]"),
            "channel": channel,
        })
    return items


# ---- render ----------------------------------------------------------------------------------

def render(items, start_date, end_date):
    by_cat = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    blocks = []
    for key, emoji, heading in CATEGORIES:
        group = by_cat.get(key)
        if not group:
            continue
        lines = [f"{emoji} *{heading}*"]
        for it in group:
            line = f"• *{it['title']}*"
            if it.get("summary"):
                line += f" — {it['summary']}"
            lines.append(line)
            if it.get("url"):
                lines.append(it["url"])
            lines.append(f"  _{it['channel']}_")
        blocks.append("\n".join(lines))
    header = (
        f"🧠 *AI digest* — {start_date.strftime('%d %b')} → {end_date.strftime('%d %b')}  "
        f"({len(items)} item{'s' if len(items) != 1 else ''})"
    )
    return header + "\n\n" + "\n\n".join(blocks)


def split_for_whatsapp(text, limit=3500):
    """Split a long digest on blank-line boundaries so each WhatsApp message stays readable."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = block if not cur else cur + "\n\n" + block
    if cur:
        parts.append(cur)
    n = len(parts)
    return [f"{p}\n\n_({i + 1}/{n})_" for i, p in enumerate(parts)]


# ---- main ------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(TD.get("lookback_days", 7)))
    ap.add_argument("--dry-run", action="store_true", help="print digest, do not send or save state")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    now = datetime.now(TZ)
    start_date = (now - timedelta(days=args.days)).date()
    end_date = now.date()
    seen = {} if args.dry_run else load_seen()

    fresh, raw_count = [], 0
    for jid, name in TD.get("channels", {}).items():
        rows = fetch_messages(jid, args.days)
        lines = transcript_lines(rows)
        if not lines:
            print(f"[{name}] no messages in window", flush=True)
            continue
        batches = chunk(lines)
        print(f"[{name}] {len(lines)} msgs in {len(batches)} chunk(s)", flush=True)
        for i, b in enumerate(batches):
            items = extract_chunk(name, "\n".join(b), args.timeout)
            raw_count += len(items)
            for it in items:
                k = item_key(it)
                if k in seen:
                    continue
                seen[k] = end_date.isoformat()  # mark within-run too, so chunks don't dupe
                fresh.append(it)
            print(f"  chunk {i + 1}/{len(batches)}: +{len(items)} extracted", flush=True)

    if not fresh:
        print(f"No new items (raw extracted across runs: {raw_count}).", flush=True)
        return

    out = render(fresh, datetime.combine(start_date, datetime.min.time()),
                 datetime.combine(end_date, datetime.min.time()))
    parts = split_for_whatsapp(out)

    if args.dry_run:
        print("\n========== DIGEST (NOT sent) ==========\n")
        print(out)
        print(f"\n[{len(fresh)} new items, {len(parts)} WhatsApp message(s)]")
        return

    for p in parts:
        send_self(p)
    ttl = (end_date - timedelta(days=int(TD.get("seen_ttl_days", 120)))).isoformat()
    save_seen(prune_seen(seen, ttl))
    print(f"Sent {len(fresh)} new items in {len(parts)} message(s).", flush=True)


if __name__ == "__main__":
    main()
