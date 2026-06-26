#!/usr/bin/env python3
"""Daily per-kid digest → WhatsApp self-chat, built on the shared event ledger.

For each child, reconciles each group's recent messages (text + image/document team sheets +
Google-Sheets CSV rosters) into the persistent event ledger via headless `claude -p`, then
renders the "rest of this week (today → Sunday)" view DETERMINISTICALLY from the ledger using
the same wa_events.event_lines() the realtime alerts use — so both channels look identical and
can't disagree. One message per child is sent to the operator's own chat via the bridge REST API;
unchanged digests are suppressed.

Run manually:  python3 digest.py [lookback_days]
Scheduled via: wa-digest.timer (08:00 & 18:00 Europe/Dublin)
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import wa_events  # shared event-ledger model (single source of truth)

CFG = json.load(open(os.path.expanduser("~/.config/wa-alerts/config.json")))
DB = os.environ.get("WA_MESSAGES_DB") or CFG.get("messages_db") or os.path.expanduser(
    "~/whatsapp-mcp/whatsapp-bridge/store/messages.db"
)
LOOKBACK = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("WA_DIGEST_LOOKBACK", "10"))
TZ = ZoneInfo("Europe/Dublin")
SHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")
STATE_DIR = wa_events.STATE_DIR
MEDIA_CACHE = wa_events.MEDIA_CACHE


def _state_path(kid):
    return os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9]", "_", kid) + ".txt")


def load_prev(kid):
    try:
        with open(_state_path(kid), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def save_prev(kid, text):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_state_path(kid), "w", encoding="utf-8") as fh:
        fh.write(text)


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


def cached_media(message_id):
    """Image/document the realtime listener already saved at receive time, by message id.

    WhatsApp's CDN 403s on re-download within minutes, so /api/download is unreliable
    for team sheets; the listener persists the inline webhook bytes for reuse here.
    Matches the listener's filename convention (sanitised message id + extension).
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", message_id or "")[:60]
    if not safe:
        return None
    try:
        for name in os.listdir(MEDIA_CACHE):
            if name.rsplit(".", 1)[0] == safe:
                return os.path.join(MEDIA_CACHE, name)
    except FileNotFoundError:
        pass
    return None


def fetch_media(message_id, chat_jid):
    """Local path to an attachment: the listener's cache first, then a bridge re-download."""
    cached = cached_media(message_id)
    if cached:
        return cached
    try:
        res = bridge_post("/download", {"message_id": message_id, "chat_jid": chat_jid})
        return res.get("path") if res.get("success") else None
    except Exception:
        return None


def fetch_sheet(sid):
    """Public 'anyone with link' Google Sheets export to CSV; None if not public."""
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(200_000).decode("utf-8", "replace")
        if "text/csv" not in ctype:
            return None  # login wall / not shared publicly
        return "\n".join(line for line in data.splitlines() if line.strip(","))[:8000]
    except Exception:
        return None


def group_ignore(jid):
    """Per-group 'treat as NOT relevant' rule from config.chat_ignore (shared with listener)."""
    txt = ((CFG.get("chat_ignore") or {}).get(jid, "") or "").strip()
    return f"\nALSO treat as NOT relevant for this group: {txt}" if txt else ""


def kid_groups():
    out = {}
    for jid, kid in (CFG.get("chat_kids") or {}).items():
        out.setdefault(kid, []).append(jid)
    return out


def build_context(jids):
    c = sqlite3.connect(DB)
    names = CFG.get("target_names", {})
    lines, sheet_ids, attachments = [], [], 0
    for jid in jids:
        rows = c.execute(
            "select id, datetime(timestamp), sender, content, media_type "
            "from messages where chat_jid=? and timestamp >= datetime('now', ?) order by timestamp",
            (jid, f"-{LOOKBACK} days"),
        ).fetchall()
        if not rows:
            continue
        lines.append(f"\n===== GROUP: {names.get(jid, jid)} =====")
        for mid, ts, sender, content, mtype in rows:
            who = "+" + re.sub(r"\D", "", sender or "")[-4:]
            if mtype in ("image", "document"):
                # Team sheets / squad lists arrive as images OR PDFs; the listener cached the
                # bytes (CDN 403s on re-download), so fetch_media() finds it by message id.
                label = "IMAGE" if mtype == "image" else "DOCUMENT"
                path = fetch_media(mid, jid)
                if path:
                    attachments += 1
                    lines.append(f"[{ts}] {who}: [{label} attached: {path}] {content or ''}".rstrip())
                else:
                    lines.append(f"[{ts}] {who}: [{label.lower()} - unavailable] {content or ''}".rstrip())
            elif content:
                lines.append(f"[{ts}] {who}: {content}")
                for sid in SHEET_RE.findall(content):
                    if sid not in sheet_ids:
                        sheet_ids.append(sid)
    c.close()
    return "\n".join(lines), sheet_ids, attachments


# The model EXTRACTS structured events from one group's transcript; CODE merges them into the
# ledger and renders the digest. The model never formats the output.
RECONCILE_PROMPT = """You are a parent's assistant. Below are recent WhatsApp messages from the
group "{group}" (for {kid}'s team). Today is {today}.
Extract EVERY training session or match dated between {start} and {end} inclusive, as structured
events. Each message is prefixed with its send time [YYYY-MM-DD HH:MM]; resolve relative words
("tonight"/"today"/"tomorrow"/a weekday) against THAT message's own send date, and write absolute
dates. If a message's WEEKDAY and date-number disagree (e.g. "Saturday 12th" when the 12th is a
Friday), trust the WEEKDAY. When details conflict, use the MOST RECENT message.
Read any "[IMAGE attached: <path>]" / "[DOCUMENT attached: <path>]" files and the CSV TEAM SHEETS
to find {kid}'s bib/team colour — use "" (not a guess) if unclear.
Set status="cancelled" for cancelled/called-off events and KEEP them (the parent wants to know
nothing is on); use "postponed" for postponements.
If a message includes a Google Maps / location link for the venue (e.g. maps.app.goo.gl/…,
goo.gl/maps/…, maps.google.com/…, or a "https://maps…?q=…" pin), copy that URL EXACTLY into
map_url — do not shorten, paraphrase, or invent one; use "" when no link is present.
Ignore banter, results, availability chatter (who's in/out), and photos with no logistics.
NEVER invent a time or venue a message doesn't state.{extra_ignore}

IMPORTANT:
- Output ONE event per real session/match — do NOT split a single one into several entries;
  merge all of its details (squad, meet time, jerseys, etc.) into that one event.
- At most ONE event per calendar day for THIS group (combine same-day items).
- Include ONLY events for THIS group's own team; ignore other teams' fixtures mentioned in passing.
- "notes" = parent-actionable essentials only (bring/meet/carpool, cancellation reason). NEVER
  explain your date reasoning or mention which message said what.

ALREADY-KNOWN events for {kid} in THIS group (use only to FILL missing detail — do NOT
re-output any event you can't find in the TRANSCRIPT below):
{ledger}

Output ONLY a JSON array — no analysis, no prose, no markdown fences, nothing before or after it.
Each element exactly:
{{"iso":"YYYY-MM-DD","date":"Sat 14 Jun","type":"training|match|blitz|tournament|other",
  "emoji":"⚽|🏑|🏆","title":"short title","time":"","place":"","map_url":"","bib":"","team":"",
  "notes":"","status":"scheduled|cancelled|postponed"}}
Use "" for any unknown field. Return [] if nothing falls in range.

TRANSCRIPT:
{body}

TEAM SHEETS (CSV):
{sheets}"""


def send_self(text):
    bridge_post("/send", {"recipient": CFG["self_jid"], "message": text}, timeout=20)


def reconcile_group(kid, group, body, sheets, ledger, today, start, end, extra_ignore, attachments):
    """Ask the model for the structured events in one group's transcript; return a list of dicts."""
    prompt = RECONCILE_PROMPT.format(
        kid=kid, group=group, today=today, start=start, end=end, body=body, sheets=sheets,
        ledger=wa_events.ledger_summary(ledger), extra_ignore=extra_ignore,
    )
    cmd = ["claude", "-p", prompt]
    if attachments:
        cmd += ["--allowedTools", "Read"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=480, cwd="/tmp")
    raw = (proc.stdout or "").strip()
    # Match an array OF OBJECTS specifically, so an echoed "[document - unavailable]" in the
    # transcript can't be mistaken for the result. Empty/parse-miss both yield [] (== no events).
    m = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        print(f"[{kid}/{group}] bad JSON: {m.group(0)[:160]!r}", flush=True)
        return []
    return arr if isinstance(arr, list) else []


def render_week(ledger, start_iso, end_iso):
    """Deterministically render the ledger for [start,end] — same event_lines() the alerts use."""
    days = {}
    for e in ledger:
        iso = e.get("iso") or ""
        if start_iso <= iso <= end_iso:
            days.setdefault(iso, []).append(e)
    blocks = []
    for iso in sorted(days):
        head = datetime.strptime(iso, "%Y-%m-%d").strftime("%A %d %B")
        lines = [f"**{head}**"]
        evs = sorted(days[iso], key=lambda x: (x.get("status") == "cancelled",
                                               x.get("time") or "~", x.get("title") or ""))
        for e in evs:
            lines.append("")
            lines += wa_events.event_lines(e, with_title=True)
        timed = [e for e in days[iso] if e.get("status") != "cancelled" and e.get("time")]
        if len(timed) >= 2:
            clash = " vs ".join(f"{e.get('title', 'event')} ({e['time']})" for e in timed)
            lines += ["", f"⚠️ Clash: {clash} — can't make both."]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def main():
    now = datetime.now(TZ).date()
    today = now.strftime("%A %d %B %Y")
    start_iso = now.isoformat()
    end_date = now + timedelta(days=(6 - now.weekday()))  # upcoming Sunday (today if Sun)
    end_iso = end_date.isoformat()
    names = CFG.get("target_names", {})
    for kid, jids in kid_groups().items():
        try:
            # Reconcile each group's messages into the kid's ledger (the single source of truth).
            ledger = wa_events.prune_ledger(
                wa_events.load_ledger(kid), (now - timedelta(days=2)).isoformat()
            )
            for jid in jids:
                group = names.get(jid, jid)
                body, sheet_ids, attachments = build_context([jid])
                if not body.strip():
                    continue
                sheets = "\n\n".join(
                    f"--- sheet {sid} ---\n{csv}"
                    for sid in sheet_ids[-6:]
                    for csv in [fetch_sheet(sid)]
                    if csv
                ) or "(none public)"
                # Only THIS group's known events as context — passing the whole kid's ledger
                # made the model re-emit other groups' events under this group's key (dupes).
                own = [e for e in ledger if e.get("group") == group]
                events = reconcile_group(
                    kid, group, body, sheets, own, today, start_iso, end_iso,
                    group_ignore(jid), attachments,
                )
                for raw in events:
                    ev = wa_events.normalize_event(raw, group=group, updated_at=now.isoformat())
                    if ev.get("iso"):
                        wa_events.upsert(ledger, ev)
            wa_events.save_ledger(kid, ledger)

            # Render the week deterministically from the ledger; skip if nothing's on / unchanged.
            out = render_week(ledger, start_iso, end_iso)
            if not out:
                print(f"[{kid}] nothing in window — skipped", flush=True)
                continue
            if load_prev(kid).strip() == out.strip():
                print(f"[{kid}] no change — skipped", flush=True)
                continue
            send_self(f"📅 This week — {kid} (through Sun {end_date.strftime('%d %b')})\n\n{out}")
            save_prev(kid, out)
            print(f"[{kid}] sent ({len(ledger)} events in ledger)", flush=True)
        except Exception as exc:
            print(f"[{kid}] error: {exc!r}", flush=True)


if __name__ == "__main__":
    main()
