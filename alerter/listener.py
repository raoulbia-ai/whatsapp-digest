#!/usr/bin/env python3
"""WhatsApp kids' training/matches realtime alerter.

Receives bridge webhooks for incoming messages (text, image AND document attachments),
keeps only the configured team group(s), and asks headless `claude -p` to pull
out ONLY the parent-actionable details — date, time, place, bib/strip colour,
team/squad, and anything else essential. Image attachments (team sheets, fixtures)
are read with vision and document attachments (e.g. PDF squad lists) with the Read
tool. Relevant alerts go to the operator's own "message yourself" chat via the
bridge REST API, tagged with the child's name.

Config is reloaded on every request, so editing config.json takes effect live.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import wa_events  # shared event-ledger model (single source of truth)

TZ = ZoneInfo("Europe/Dublin")

CONFIG_PATH = os.environ.get(
    "WA_ALERT_CONFIG", os.path.expanduser("~/.config/wa-alerts/config.json")
)

# Persisted media cache: the webhook delivers image/document bytes inline, but WhatsApp's
# CDN 403s on any later re-download — so we save them here for the daily digest to reuse,
# keyed by message id. Pruned past WhatsApp's ~14-day media lifetime.
MEDIA_CACHE = wa_events.MEDIA_CACHE
MEDIA_MAX_AGE_DAYS = 16
_locks_guard = threading.Lock()
_locks = {}


def kid_lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())

# The model EXTRACTS one structured event; CODE owns the key and the new/changed/unchanged
# decision (by diffing against the ledger). The model never formats the alert.
INSTRUCTIONS = """You are a parent's assistant watching the WhatsApp group "{group}" for {who}.
From the LATEST message, identify the ONE training session or match it concerns and return it as
a structured event. Today / now is {now} (Europe/Dublin) — resolve relative dates ("tonight",
"this evening", "today", "tomorrow", a bare weekday) to an absolute date against THIS moment.
If a message's weekday and date-number disagree (e.g. "Saturday 12th" when the 12th is a Friday),
trust the WEEKDAY.

You are given the parent's CURRENT KNOWN EVENTS for {who} (the ledger). Use it to:
  - recognise when this message concerns an event already known (same date + team) — then return
    that event with the new detail merged in (a time/venue change, a bib/team sheet, a cancellation);
  - fill fields you can from the ledger so the event is complete — but NEVER invent a time or
    venue the message and ledger don't actually state;
  - judge relevance: set relevant=false for banter, thanks, results, photos with no logistics,
    pure availability chatter (who's in/out), or anything not actionable for a parent.{extra_ignore}

Set status="cancelled" when the message cancels/calls off the event (keep its date + team so it
is recorded — "no game" is worth knowing). Use status="postponed" for postponements.
If the message includes a Google Maps / location link for the venue (e.g. maps.app.goo.gl/…,
goo.gl/maps/…, maps.google.com/…, or a "https://maps…?q=…" pin), copy that URL EXACTLY into
map_url — do not shorten, paraphrase, or invent one; use "" when no link is present.

CURRENT KNOWN EVENTS for {who}:
{ledger}
{media_note}
Message sender: {sender}
Caption / text: \"\"\"{content}\"\"\"

Respond with ONLY this JSON object and nothing else:
{{"relevant": <true|false>,
  "event": {{
    "iso": "<YYYY-MM-DD>",
    "date": "<e.g. Sun 14 Jun>",
    "type": "<training|match|blitz|tournament|other>",
    "emoji": "<⚽ football | 🏑 hurling | 🏆 match/blitz/tournament>",
    "title": "<short event title, e.g. 'League match away v Knockmitten'>",
    "time": "<e.g. 10:30am (arrive 10:00am), or empty>",
    "place": "<venue + pitch/hall, or empty>",
    "map_url": "<Google Maps / location link for the venue copied verbatim, or empty>",
    "bib": "<bib/strip colour for {who}, or empty>",
    "team": "<team/squad name, or empty>",
    "notes": "<parent-actionable essentials in ONE short line: bring/meet/carpool/cancellation reason; NO date-reasoning meta, or empty>",
    "status": "<scheduled|cancelled|postponed>"
  }}
}}
If relevant=false you may leave event fields empty. "iso" is REQUIRED when relevant=true."""


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def read_token(cfg):
    tok = (cfg.get("bridge_token") or "").strip()
    if tok:
        return tok
    with open(cfg["bridge_token_path"], encoding="utf-8") as fh:
        return fh.read().strip()


def keyword_hit(content, cfg):
    kws = cfg.get("prefilter_keywords")
    if not kws:
        return True
    text = content.lower()
    return any(k.lower() in text for k in kws)


def prune_media_cache():
    """Drop cached media (images/documents) older than WhatsApp's media lifetime."""
    cutoff = time.time() - MEDIA_MAX_AGE_DAYS * 86400
    try:
        for name in os.listdir(MEDIA_CACHE):
            p = os.path.join(MEDIA_CACHE, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except FileNotFoundError:
        pass


def media_ext(payload):
    """Pick a file extension from MIME, falling back to the WhatsApp filename, then type."""
    mime = (payload.get("mimeType") or "").split(";")[0].strip()
    by_mime = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "application/pdf": ".pdf",
    }
    if mime in by_mime:
        return by_mime[mime]
    m = re.search(r"(\.[A-Za-z0-9]{1,8})$", payload.get("mediaFilename") or "")
    if m:
        return m.group(1).lower()
    return ".pdf" if payload.get("mediaType") == "document" else ".jpg"


def save_media(payload):
    """Decode an image or document webhook to the persistent media cache; return path or None.

    Saved (not /tmp, not deleted after use) so the daily digest can re-read team sheets:
    the webhook carries the bytes inline, but WhatsApp's CDN rejects re-download (403)
    within minutes. Keyed by message id so the digest can look it up. Documents (e.g. PDF
    team sheets) are read by `claude -p` with the Read tool, same as images with vision.
    """
    b64 = payload.get("mediaBase64")
    if payload.get("mediaType") not in ("image", "document") or not b64:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", payload.get("messageId", "media"))[:60]
    os.makedirs(MEDIA_CACHE, exist_ok=True)
    path = os.path.join(MEDIA_CACHE, f"{safe}{media_ext(payload)}")
    try:
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(b64))
        prune_media_cache()
        return path
    except Exception as exc:
        print(f"[warn] could not save media: {exc!r}", flush=True)
        return None


def claude_extract(content, sender, chat, cfg, media_path=None, ledger=None):
    """Return (relevant: bool, event: dict). Handles text, image (vision) and document (Read).

    The model extracts ONE structured event against the current ledger; CODE then decides
    new/changed/unchanged by diffing, so relevance is the model's only judgement call here.
    """
    kid = (cfg.get("chat_kids") or {}).get(chat, "")
    group = (cfg.get("target_names") or {}).get(chat, "the team")
    who = kid or "the team"
    is_doc = bool(media_path) and media_path.lower().endswith(
        (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv")
    )
    if not media_path:
        media_note = ""
    elif is_doc:
        media_note = (
            f"\nA document is attached at {media_path} — READ it in full; it is usually a "
            f"team sheet / squad list / fixtures with meeting times. Find {who}'s entry "
            "(bib/team/meet time) and combine it with any caption above."
        )
    else:
        media_note = (
            f"\nAn image is attached at {media_path} — READ it; team sheets and fixtures "
            "are usually images. Combine it with any caption above."
        )
    extra = ((cfg.get("chat_ignore") or {}).get(chat, "") or "").strip()
    extra_ignore = (
        f"\nADDITIONALLY for THIS group, treat as NOT relevant (set relevant=false): {extra}"
        if extra
        else ""
    )
    now = datetime.now(TZ).strftime("%A %d %b %Y, %H:%M")
    prompt = INSTRUCTIONS.format(
        group=group, who=who, sender=sender, content=content or "(none)",
        media_note=media_note, now=now, extra_ignore=extra_ignore,
        ledger=wa_events.ledger_summary(ledger or []),
    )
    cmd = ["claude", "-p", prompt]
    if media_path:
        cmd += ["--allowedTools", "Read"]  # let it read the local image/document file
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=cfg.get("claude_timeout", 180 if media_path else 120),
        cwd=cfg.get("claude_cwd", "/tmp"),  # neutral cwd: skip loading project MCP
    )
    raw = (proc.stdout or "").strip()
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        print(f"[warn] no JSON from claude: {raw[:160]!r}", flush=True)
        return False, {}
    try:
        obj = json.loads(match.group(0))
    except Exception:
        print(f"[warn] bad JSON from claude: {match.group(0)[:160]!r}", flush=True)
        return False, {}
    return bool(obj.get("relevant")), (obj.get("event") or {})


def send_self(text, cfg):
    body = json.dumps({"recipient": cfg["self_jid"], "message": text}).encode()
    req = urllib.request.Request(
        cfg["bridge_url"].rstrip("/") + "/send",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + read_token(cfg),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def process(payload):
    media_path = None
    try:
        cfg = load_config()
        if payload.get("eventType") == "reaction" or payload.get("isFromMe"):
            return
        chat = payload.get("chatJID", "")
        targets = cfg.get("target_chats") or []
        if targets and chat not in targets:
            return
        content = (payload.get("content") or "").strip()
        media_path = save_media(payload)

        # Text-only messages get a cheap keyword pre-filter; image/document attachments are
        # always analysed (a team sheet often arrives with a thin or empty caption).
        if not media_path:
            if not content:
                return
            if not keyword_hit(content, cfg):
                return

        kid = (cfg.get("chat_kids") or {}).get(chat, "")
        group = (cfg.get("target_names") or {}).get(chat, "")
        skey = kid or chat
        tag = {"image": "img", "document": "doc"}.get(payload.get("mediaType"), "txt")
        # Serialize per kid so concurrent messages upsert the ledger one at a time (and so two
        # near-simultaneous messages about the same event can't both alert as "new").
        with kid_lock(skey):
            ledger = wa_events.load_ledger(skey)
            relevant, event = claude_extract(
                content, payload.get("sender", ""), chat, cfg, media_path, ledger
            )
            if not relevant or not event.get("iso"):
                print(f"[skip:{tag}] {chat}: {content[:60]!r}", flush=True)
                return

            now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            ev = wa_events.normalize_event(event, group=group, updated_at=now_str)
            change, merged = wa_events.upsert(ledger, ev)
            if change == "unchanged":
                print(f"[skip:dup] {chat} ({kid}): {merged.get('key')}", flush=True)
                return
            wa_events.save_ledger(skey, ledger)

            prefix = cfg.get("alert_prefix", "🔔")
            bits = [b for b in (kid, group) if b]
            header = f"{prefix} {' · '.join(bits)}".strip()
            alert = "\n".join(
                wa_events.event_lines(merged, updated=(change == "changed"), with_title=False)
            )
            send_self(f"{header}\n{alert}", cfg)
            print(f"[alert:{change}] {chat} ({kid}): {merged.get('key')}", flush=True)
    except Exception as exc:
        print(f"[error] {exc!r}", flush=True)
    # media_path is kept in the persistent media cache (pruned by age) so the daily
    # digest can re-read team sheets; WhatsApp's CDN 403s on re-download.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        # Acknowledge immediately so the bridge's 30s webhook timeout never trips;
        # the slow claude call then runs in a worker thread.
        self.send_response(200)
        self.end_headers()
        try:
            payload = json.loads(raw)
        except Exception:
            return
        threading.Thread(target=process, args=(payload,), daemon=True).start()


if __name__ == "__main__":
    cfg = load_config()
    host = cfg.get("bind", "127.0.0.1")
    port = int(cfg.get("port", 8769))
    path = cfg.get("webhook_path", "/whatsapp/webhook")
    print(f"wa-alerts listener on http://{host}:{port}{path}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
