"""Shared event-ledger model for the wa-alerts realtime listener and daily digest.

The ledger is the single source of truth for each child's known events. Each event is one
record keyed by `iso-date | group-slug | type`, updated in place as messages arrive. Both the
realtime alert and the daily digest RENDER from these records via event_lines(), so formatting
is consistent and the two channels cannot disagree. Cancellations are retained (status=
"cancelled") for reference — "no game" is itself actionable.

The MODEL only ever extracts event fields; CODE owns the key (from iso + group + type) and the
new/changed/unchanged decision (by diffing against the ledger), so neither depends on the model
being self-consistent across calls.
"""
import json
import os
import re
import tempfile

STATE_DIR = os.path.expanduser("~/.local/share/wa-alerts/state")
MEDIA_CACHE = os.path.expanduser("~/.local/share/wa-alerts/media")

# Persisted fields, in render-ish order.
EVENT_FIELDS = (
    "key", "iso", "date", "type", "emoji", "title",
    "time", "place", "map_url", "bib", "team", "notes", "status",
    "group", "updated_at",
)

# Fields whose change is "material" — i.e. worth a realtime (updated) alert. Notes/title are
# updated silently (phrasing drifts), so they don't on their own trigger a re-alert. A venue
# location pin (map_url) appearing or changing is material — it's a "where" change.
TRIGGER_FIELDS = ("iso", "date", "time", "place", "map_url", "bib", "team", "status")

STATUS_EMOJI = {"cancelled": "🚫", "postponed": "⏸️"}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:24] or "x"


def event_key(iso, group):
    """Stable key — code computes it from group (always known) + iso, never the model.

    Deliberately NOT keyed on event type: one slot per group per day, so a match the model
    later re-labels "game"/"blitz" updates the same record instead of spawning a duplicate.
    A group rarely has two distinct events on one day; if it does, they merge (better than dupes).
    """
    return f"{iso or 'nodate'}|{_slug(group)}"


def ledger_path(kid):
    return os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9]", "_", kid) + ".events.json")


def load_ledger(kid):
    try:
        with open(ledger_path(kid), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_ledger(kid, events):
    """Atomic write so a listener/digest race can't corrupt the file (lost update is re-derived)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = ledger_path(kid)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".evt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(events, fh, ensure_ascii=False, indent=0)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def normalize_event(raw, group="", updated_at=""):
    """Coerce a model-emitted event dict into a ledger record; CODE assigns group + key."""
    out = {k: (raw.get(k) or "") for k in EVENT_FIELDS}
    out["group"] = group or raw.get("group") or ""
    out["status"] = (raw.get("status") or "scheduled").lower().strip()
    out["type"] = (raw.get("type") or "event").lower().strip()
    out["updated_at"] = updated_at
    out["key"] = event_key(out["iso"], out["group"])
    return out


def upsert(events, ev):
    """Insert or merge by key. Returns ('new'|'changed'|'unchanged', merged_event).

    Merge keeps existing fields where the incoming value is empty (so partial updates — e.g. a
    bib-only team sheet — don't wipe a known time/venue). 'changed' is decided on TRIGGER_FIELDS
    only; notes/title refresh silently.
    """
    for i, e in enumerate(events):
        if e.get("key") == ev["key"]:
            merged = dict(e)
            for k in EVENT_FIELDS:
                if k in ("key",):
                    continue
                if ev.get(k):
                    merged[k] = ev[k]
            material = any((merged.get(k) or "") != (e.get(k) or "") for k in TRIGGER_FIELDS)
            events[i] = merged
            return ("changed" if material else "unchanged"), merged
    events.append(ev)
    return "new", ev


def prune_ledger(events, before_iso):
    """Drop dated events strictly older than before_iso (keeps the file small)."""
    return [e for e in events if not e.get("iso") or e.get("iso") >= before_iso]


def event_lines(ev, updated=False, with_title=True):
    """Render one event to display lines, shared by both channels so they read identically.

    with_title=True  (digest): a day header already supplies the date → emit a "<emoji> title"
                               line and NO 📅 line.
    with_title=False (realtime alert): no header → lead with the 📅 date line instead.
    """
    cancelled = ev.get("status") == "cancelled"
    postponed = ev.get("status") == "postponed"
    upd = " (updated)" if updated else ""
    lines = []
    if with_title:
        emoji = STATUS_EMOJI.get(ev.get("status")) or ev.get("emoji") or "•"
        title = ev.get("title") or ev.get("type", "event").title()
        if cancelled:
            title += " — CANCELLED"
        elif postponed:
            title += " — POSTPONED"
        lines.append(f"{emoji} {title}{upd}")
    else:
        date = ev.get("date") or ev.get("iso")
        if date:
            lines.append(f"📅 {date}{upd}")
        if cancelled:
            lines.append(f"🚫 {ev.get('title') or 'Event'} — cancelled")
        elif postponed:
            lines.append(f"⏸️ {ev.get('title') or 'Event'} — postponed")
    if not cancelled:
        if ev.get("time"):
            lines.append(f"🕒 {ev['time']}")
        if ev.get("place"):
            lines.append(f"📍 {ev['place']}")
        if ev.get("map_url"):
            lines.append(f"🗺️ {ev['map_url']}")
        if ev.get("bib"):
            lines.append(f"🎽 {ev['bib']}")
    if ev.get("team"):
        lines.append(f"👥 {ev['team']}")
    if ev.get("notes"):
        lines.append(f"📝 {ev['notes']}")
    return lines


def ledger_summary(events):
    """Compact one-line-per-event view of the ledger, for prompting the model."""
    if not events:
        return "(no events known yet)"
    out = []
    for e in sorted(events, key=lambda x: (x.get("iso") or "", x.get("group") or "")):
        head = " · ".join(filter(None, [
            e.get("date") or e.get("iso") or "?",
            e.get("group") or "",
            e.get("type") or "",
            (e.get("status") or "").upper() if e.get("status") not in ("", "scheduled") else "",
        ]))
        detail = " / ".join(filter(None, [e.get("time"), e.get("place"), e.get("bib"), e.get("notes")]))
        out.append(f"- {head}" + (f" — {detail}" if detail else ""))
    return "\n".join(out)
