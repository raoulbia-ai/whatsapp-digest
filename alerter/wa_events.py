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
import subprocess
import tempfile

STATE_DIR = os.path.expanduser("~/.local/share/wa-alerts/state")
MEDIA_CACHE = os.path.expanduser("~/.local/share/wa-alerts/media")

# Persisted fields, in render-ish order.
EVENT_FIELDS = (
    "key", "iso", "date", "type", "sport", "emoji", "title",
    "time", "place", "map_url", "bib", "team", "notes", "status",
    "parts", "group", "src", "updated_at",
)

# One record per group per day (see event_key), so a day carrying several fixtures for different
# squads — "Div 1 at 2pm in Ballyboden, Div 7 at 1:30pm on pitch 21" — used to keep one squad's
# time/venue at the top and push the others into notes as prose. `parts` holds them as structured
# sub-entries instead: [{"label","time","place","status"}], rendered one bullet each.
PART_FIELDS = ("label", "time", "place", "status")

# Sport is decided by the CHAT (config.chat_sports), not guessed by the model — each group is a
# single club code. GAA groups run both codes, so there the message text picks football/hurling.
# "football" is deliberately not a value: it means soccer in one group and gaelic in another.
SPORT_EMOJI = {"soccer": "\u26bd", "gaa-football": "\U0001f3d0", "hurling": "\U0001f3d1"}
SPORT_LABEL = {"soccer": "Soccer", "gaa-football": "GAA football", "hurling": "Hurling"}


def sport_emoji(sport, fallback=""):
    return SPORT_EMOJI.get((sport or "").lower().strip()) or fallback or "\u2022"


# Fields whose change is "material" — i.e. worth interrupting the user for. Deliberately narrow:
# WHEN it is and WHETHER it is on. Everything else (place, map_url, bib, team, notes, title) is
# free text the model re-extracts per message, so ordinary phrasing drift — "Woodside" becoming
# "Woodside, St Anne's" — used to fire a full re-alert. Those now refresh the ledger silently and
# surface in the next digest.
TRIGGER_FIELDS = ("iso", "date", "time", "status")

STATUS_EMOJI = {"cancelled": "🚫", "postponed": "⏸️"}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:24] or "x"


def chat_slug(jid):
    """Short stable identifier for a chat, derived from the JID's numeric id."""
    return (jid or "").split("@")[0].split("-")[0][-10:] or "x"


def event_key(iso, group, jid=""):
    """Stable key — code computes it from the chat + iso, never the model.

    Keyed on the chat JID when known, NOT the group name: WhatsApp group subjects get renamed
    every season ("U12" -> "U13"), and a name-derived key would orphan every existing record on
    the rename, re-alerting known events as new. Falls back to the name slug for old records.

    Deliberately NOT keyed on event type: one slot per group per day, so a match the model
    later re-labels "game"/"blitz" updates the same record instead of spawning a duplicate.
    A group rarely has two distinct events on one day; if it does, they merge (better than dupes).
    """
    return f"{iso or 'nodate'}|{chat_slug(jid) if jid else _slug(group)}"


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


def normalize_parts(raw_parts):
    """Coerce the model's sub-fixture list into clean records; drop anything unlabelled."""
    parts = []
    for p in raw_parts if isinstance(raw_parts, list) else []:
        if not isinstance(p, dict):
            continue
        rec = {k: str(p.get(k) or "").strip() for k in PART_FIELDS}
        if rec["label"]:
            parts.append(rec)
    return parts


def normalize_event(raw, group="", updated_at="", jid="", src=""):
    """Coerce a model-emitted event dict into a ledger record; CODE assigns group + key."""
    out = {k: (raw.get(k) or "") for k in EVENT_FIELDS}
    out["parts"] = normalize_parts(raw.get("parts"))
    out["group"] = group or raw.get("group") or ""
    out["status"] = (raw.get("status") or "scheduled").lower().strip()
    out["type"] = (raw.get("type") or "event").lower().strip()
    out["sport"] = (raw.get("sport") or "").lower().strip()
    # CODE assigns the emoji from the sport, so a renamed group or a model that free-associates
    # an emoji can't relabel a GAA fixture with a soccer ball.
    out["emoji"] = sport_emoji(out["sport"], raw.get("emoji") or "")
    out["updated_at"] = updated_at
    out["src"] = src or raw.get("src") or ""  # message that produced it, for edit retraction
    out["key"] = event_key(out["iso"], out["group"], jid)
    return out


def upsert(events, ev):
    """Insert or merge by key. Returns ('new'|'changed'|'unchanged', merged_event, deltas).

    Merge keeps existing fields where the incoming value is empty (so partial updates — e.g. a
    bib-only team sheet — don't wipe a known time/venue). 'changed' is decided on TRIGGER_FIELDS
    only; everything else refreshes silently. `deltas` maps each materially changed field to
    (old, new) so the alert can say what actually moved instead of just "(updated)".
    """
    for i, e in enumerate(events):
        if e.get("key") == ev["key"]:
            merged = dict(e)
            for k in EVENT_FIELDS:
                if k in ("key",):
                    continue
                if ev.get(k):
                    merged[k] = ev[k]
            deltas = {
                k: ((e.get(k) or ""), (merged.get(k) or ""))
                for k in TRIGGER_FIELDS
                if (merged.get(k) or "") != (e.get(k) or "")
            }
            events[i] = merged
            return ("changed" if deltas else "unchanged"), merged, deltas
    events.append(ev)
    return "new", ev, {}


def prune_ledger(events, before_iso):
    """Drop dated events strictly older than before_iso (keeps the file small)."""
    return [e for e in events if not e.get("iso") or e.get("iso") >= before_iso]


def _squash(s):
    """Lowercase, alphanumerics only — so "Div 1" and "Div1" compare equal."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _restates_parts(notes, parts):
    """True if the notes line is mostly a prose rerun of the part bullets.

    The model often fills both — "Div 1: 2pm Ballyboden" as a part AND "Div1 Ballyboden 2pm - ..."
    as notes — which printed the same fixtures twice under each other. Compare squashed, because
    the prose spelling ("Div1") rarely matches the label's ("Div 1").
    """
    flat = _squash(notes)
    labelled = sum(1 for p in parts if p.get("label") and _squash(p["label"]) in flat)
    return labelled >= 2 or (labelled == 1 and len(parts) == 1)


def _strip_part_list(title, parts):
    """Drop a trailing "(Div 1 / Div 7 / Div 10)" that the bullets already spell out."""
    m = re.search(r"\s*[(\[][^()\[\]]*[)\]]\s*$", title or "")
    if not m:
        return title
    inner = _squash(m.group(0))
    if sum(1 for p in parts if p.get("label") and _squash(p["label"]) in inner) >= 2:
        return title[: m.start()].rstrip()
    return title


def _same_team(team, group):
    """True if the team label adds nothing over the group name (one contains the other's words)."""
    a, b = set(re.findall(r"[a-z0-9]+", (team or "").lower())), set(
        re.findall(r"[a-z0-9]+", (group or "").lower()))
    return bool(a) and bool(b) and (a <= b or b <= a)


def event_lines(ev, updated=False, with_title=True, deltas=None, brief=False):
    """Render one event to display lines, shared by both channels so they read identically.

    with_title=True  (digest): a day header already supplies the date → emit a "<emoji> title"
                               line and NO 📅 line.
    with_title=False (realtime alert): no header → lead with the 📅 date line, but ALWAYS name
                               the event too. Emitting only "📅 Fri 5 Sep (updated)" told the
                               user something moved without saying what.
    deltas: {field: (old, new)} from upsert() — rendered as "old → new" so an update is
                               self-explanatory.
    brief:  digest mode — drop the team line (the header already names the group).
    """
    cancelled = ev.get("status") == "cancelled"
    postponed = ev.get("status") == "postponed"
    upd = " (updated)" if updated else ""
    deltas = deltas or {}
    lines = []
    emoji = STATUS_EMOJI.get(ev.get("status")) or ev.get("emoji") or "•"
    title = ev.get("title") or ev.get("type", "event").title()
    if ev.get("parts"):
        title = _strip_part_list(title, ev["parts"])
    if with_title:
        if cancelled:
            title += " — CANCELLED"
        elif postponed:
            title += " — POSTPONED"
        lines.append(f"{emoji} {title}{upd}")
    else:
        date = ev.get("date") or ev.get("iso")
        if date:
            old_date = (deltas.get("date") or deltas.get("iso") or (None, None))[0]
            shown = f"{old_date} → {date}" if old_date else date
            lines.append(f"📅 {shown}{upd}")
        # Always identify the event, not only when it is off.
        if cancelled:
            lines.append(f"🚫 {ev.get('title') or 'Event'} — cancelled")
        elif postponed:
            lines.append(f"⏸️ {ev.get('title') or 'Event'} — postponed")
        else:
            lines.append(f"{emoji} {title}")
    parts = ev.get("parts") or []
    if not cancelled:
        # With sub-fixtures, a single 🕒/📍 would be one squad's detail presented as the day's —
        # exactly the confusion the bullets exist to remove. Each part carries its own.
        if parts:
            for p in parts:
                detail = ", ".join(filter(None, (p.get("time"), p.get("place")))) or "TBC"
                state = (p.get("status") or "").lower()
                mark = f" — {state.upper()}" if state in ("cancelled", "postponed") else ""
                lines.append(f"   • {p['label']}: {detail}{mark}")
        else:
            if ev.get("time"):
                old_time = (deltas.get("time") or (None, None))[0]
                lines.append(f"🕒 {old_time} → {ev['time']}" if old_time else f"🕒 {ev['time']}")
            if ev.get("place"):
                lines.append(f"📍 {ev['place']}")
        if ev.get("map_url"):
            lines.append(f"🗺️ {ev['map_url']}")
        if ev.get("bib"):
            lines.append(f"🎽 {ev['bib']}")
    # Suppress the team line when it just restates the group already in the header/day block —
    # "Clontarf GAA - Boys 2016" followed by "👥 Boys 2016" is a wasted line in every alert.
    team = (ev.get("team") or "").strip()
    if team and not brief and not _same_team(team, ev.get("group")):
        lines.append(f"👥 {team}")
    notes = (ev.get("notes") or "").strip()
    if notes and parts and _restates_parts(notes, parts):
        notes = ""  # the bullets already say it — don't print the prose version underneath
    if notes:
        lines.append(f"📝 {notes}")
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


# ---- Claude CLI ------------------------------------------------------------------------------
#
# Both digests shell out to `claude -p`. A broken CLI (expired credential, missing binary, crash)
# exits fast with empty stdout, which is indistinguishable from "the model found nothing" unless
# we look. Silently treating that as an empty result once cost three weeks of missed digests, so
# run_claude() RAISES on a failed invocation and the callers turn that into a visible alert.

class ClaudeCLIError(RuntimeError):
    """`claude -p` did not produce usable output (auth, crash, timeout, empty response)."""


def run_claude(cmd, timeout, cwd="/tmp"):
    """Run a `claude -p` command and return stdout. Raises ClaudeCLIError on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise ClaudeCLIError("claude CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise ClaudeCLIError(f"claude CLI timed out after {timeout}s")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise ClaudeCLIError(f"claude CLI exited {proc.returncode}: {(err or out)[:300]}")
    if not out:
        raise ClaudeCLIError(f"claude CLI returned empty output: {err[:300]}")
    # An expired login exits 0 on some versions and prints a login prompt instead of an answer.
    low = out.lower()
    if len(out) < 400 and any(p in low for p in (
        "/login", "please log in", "invalid api key", "authentication_error",
        "oauth token has expired", "credit balance is too low",
    )):
        raise ClaudeCLIError(f"claude CLI not authenticated: {out[:200]}")
    return out


def drop_by_source(events, message_id):
    """Remove events created solely by `message_id` — used when that message is EDITED.

    WhatsApp edits replace the text entirely, so an event derived from the old wording may no
    longer correspond to anything. Only records whose own source is that message are dropped;
    an event that has since been confirmed or amended by another message keeps its place.
    """
    if not message_id:
        return events, []
    dropped = [e for e in events if e.get("src") == message_id]
    if not dropped:
        return events, []
    return [e for e in events if e.get("src") != message_id], dropped
