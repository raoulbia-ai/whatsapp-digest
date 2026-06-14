# Realtime kids' training/matches alerter — implementation

A self-contained, always-on pipeline that watches specific WhatsApp **group chats**
and, the moment a message about **training or a match** arrives, pushes a concise
summary to the operator's own *"Message yourself"* WhatsApp chat.

- **Delivery:** WhatsApp → yourself (no email, push service, or API key).
- **Cadence:** realtime (event-driven via the bridge webhook).
- **Extraction brain:** headless `claude -p` (uses the local Claude Code subscription).
- **Runtime:** two systemd **user** services (survive logout/reboot via linger).

Built 2026-06-11 on a headless KVM VM. This document describes the deployment that
sits **on top of** the upstream `whatsapp-bridge` + `whatsapp-mcp-server`; the only
changes to repo code are a small pairing-code login patch (see
[§7](#7-bridge-code-patch-pairing-code-login)) and a media-forwarding change so that
**document** attachments (PDF team sheets), not just images, are downloaded and base64-pushed
to the webhook for the AI pipeline to read.

---

## 1. Architecture

```
                          WhatsApp servers
                                 │  (linked device, websocket)
                                 ▼
        ┌─────────────────────────────────────────────┐
        │  wa-bridge.service   (Go, whatsmeow)         │
        │  - REST API   127.0.0.1:8085                 │
        │  - SQLite     whatsapp-bridge/store/*.db     │
        │  - Webhook POST on every incoming message ───┼──┐
        └─────────────────────────────────────────────┘  │
                  ▲  POST /api/send (alert to self)        │ POST {sender,content,chatJID,...}
                  │                                        ▼
        ┌─────────┴───────────────────────────────────────────────┐
        │  wa-alerts.service   (Python, listener.py)               │
        │  127.0.0.1:8769/whatsapp/webhook                         │
        │  1. filter → only configured target group JIDs           │
        │  2. text → keyword pre-filter; image/doc → always analysed│
        │  3. claude -p (vision + Read PDFs) → structured event      │
        │  4. upsert event ledger → render alert if new/changed      │
        │  4. if relevant → POST alert to bridge /api/send (self),  │
        │     tagged with the child's name (Alex / Sam)            │
        └──────────────────────────────────────────────────────────┘
```

**Why this shape**
- The bridge already emits a webhook per incoming message — that's the realtime trigger,
  no polling needed.
- The listener stays dumb and stateless; all judgement is delegated to `claude -p`.
- Sending the alert back through the *same* bridge means the only delivery surface is
  WhatsApp itself — nothing new to secure or authenticate.

---

## 2. Data flow (one message)

1. A member posts in e.g. *CLONTARF BOYS U12*.
2. Bridge persists it to SQLite **and** POSTs a JSON `WebhookPayload` to
   `http://localhost:8769/whatsapp/webhook`.
3. Listener **ACKs `200` immediately** (so the bridge's 30 s webhook timeout never
   trips), then processes in a worker thread.
4. Drop if: it's a reaction, `isFromMe`, or not in `target_chats`. **Text-only**
   messages then also need a keyword pre-filter hit; **image and document** attachments skip
   the pre-filter (a team sheet often has a thin/empty caption) and are always analysed.
5. The attachment (if any) is base64-decoded to the media cache. `claude -p` is asked to
   return **only** a JSON object `{"relevant": bool, "alert": str}`; with an attachment it
   runs with `--allowedTools Read` so it can read the image with vision or the PDF/document
   with the Read tool.
6. If `relevant` and `alert` non-empty → `POST /api/send` to the operator's self-JID with
   `"<prefix> <child> · <group name>\n<alert>"`, then the temp image is deleted.
7. Bridge sends it via WhatsApp; it appears in the *Message yourself* chat.

---

## 3. Files

| Path | Role |
|---|---|
| `~/.local/share/wa-alerts/wa_events.py` | **Shared event-ledger model** — schema, key, upsert, render; imported by both listener and digest |
| `~/.local/share/wa-alerts/listener.py` | The webhook listener / extractor / ledger-updater / sender |
| `~/.config/wa-alerts/config.json` | All tunables (groups, self-JID, keywords, prompts) |
| `~/.local/share/wa-alerts/media/<message-id>.<ext>` | Cached incoming images **and documents** (PDF team sheets) for digest reuse (CDN 403s on re-download); age-pruned |
| `~/.local/share/wa-alerts/state/<kid>.events.json` | **The event ledger** — one record per event (single source of truth for both channels) |
| `~/.local/share/wa-alerts/state/<kid>.txt` | Last digest body sent (overwritten; digest no-change dedup) |
| `~/.config/systemd/user/wa-bridge.service` | Bridge unit |
| `~/.config/systemd/user/wa-alerts.service` | Listener unit |
| `whatsapp-bridge/whatsapp-bridge` | Prebuilt bridge binary the service runs |
| `whatsapp-bridge/main.go` | Patched for pairing-code login (uncommitted) |
| `.env` | Bridge config (port 8085, webhook URL, `FORWARD_SELF=false`) |

Operational files live **outside** the git repo (`~/.local/share`, `~/.config`) so the
clone stays clean; the repo holds only the one `main.go` patch + this doc.

---

## 4. The listener (`listener.py`)

Key design points:

- **`ThreadingHTTPServer`** — each webhook is handled on its own thread, so a slow
  `claude -p` (seconds) never blocks the next message.
- **ACK-then-work** — `do_POST` sends `200` and closes before starting the model call.
- **Config reloaded per request** — editing `config.json` (groups, keywords, prefix)
  takes effect with **no restart**. Only code/prompt changes need a restart.
- **Crash-proof per message** — `process()` wraps everything in try/except; one bad
  message can't take the listener down.
- **Robust extraction** — the model is asked for a single JSON object; the code
  regex-extracts the first `{...}` so any stray preamble/markdown fences are tolerated.

- **Vision + document reading for attachments** — image **and document** webhooks are
  decoded to a file and `claude -p` runs with `--allowedTools Read`: images are read with
  vision, PDFs/squad-list documents with the Read tool (`save_media()` picks the extension
  from MIME/filename so `.pdf` etc. are honoured). Both bypass the keyword pre-filter and are
  always analysed. The decoded file is **kept** in a persistent cache
  (`~/.local/share/wa-alerts/media/<message-id>.<ext>`, pruned past WhatsApp's ~14-day media
  lifetime) so the daily digest can re-read it — WhatsApp's CDN 403s on any later
  `/api/download`, but the webhook delivers the bytes inline, so caching at receive time is
  the only reliable way the digest gets team sheets. **The bridge must download+forward the
  bytes** for this to work: `main.go` does so synchronously for `image` and `document` (other
  media stays async-cache-only).
- **Per-child tagging** — each group maps to a child via `chat_kids`; the alert header is
  `<prefix> <child> · <group>` (e.g. `🔔 Alex · Example FC Under 10s`).
- **Event ledger (shared memory)** — the listener loads that child's **event ledger**
  (`state/<kid>.events.json` via `wa_events.py`) and passes a compact view to the model as the
  parent's current known state. The model EXTRACTS one structured event (date/time/place/bib/
  team/notes/status); CODE owns the key (`iso · group`) and the new/changed/unchanged decision
  by diffing against the ledger, then renders the alert. So the model doesn't have to be self-
  consistent across calls, repeats are skipped reliably, and a fixture's real time/venue is
  carried in the ledger rather than re-guessed. The **daily digest reconciles into and renders
  from the same ledger**, so the two channels can't disagree.
- **Per-group ignore** — `config.chat_ignore` (JID → free-text rule) injects an extra
  "treat as NOT relevant" clause for one group only. Used to mute U12-soccer "are any extra
  players free for Saturday?" availability/recruitment asks without affecting other groups.
  Live-tunable; the daily digest honours the same map.
- **New / changed / unchanged** — CODE diffs the extracted event against the ledger record:
  *unchanged* (no material field differs) → no alert; *new* → alert; *changed* → alert with the
  📅 line tagged **(updated)**. "Material" = date/time/place/bib/team/status; notes/title refresh
  silently so re-phrasing doesn't spam. Per-child processing is lock-serialised so two near-
  simultaneous messages about the same event can't both alert as "new".
- **Updates are self-contained** — the alert is rendered from the merged ledger record, so it
  always shows the **full current details** (new value + carried-over venue/colour/notes), and
  it does NOT narrate what changed ("was 10:00am…") — just the current state plus `(updated)`.
- **Cancellations kept** — a cancelled/postponed event stays in the ledger with
  `status=cancelled`/`postponed` and is shown (🚫 / ⏸️) in both the alert and the digest —
  "no game" is itself worth knowing.
- **Relative dates resolved** — the prompt is given the current Europe/Dublin time (realtime
  processing ≈ send time), so "this evening / tonight / today / tomorrow / Friday" become
  absolute dates (📅 Thu 11 Jun). The digest does the same against each message's `[timestamp]`.

Extraction prompt (abridged) — the model returns a **structured event**, never formatted text;
CODE renders the alert from it:

```
You are a parent's assistant watching "<group>" for <child>.
From the LATEST message, return the ONE training/match it concerns as a structured event.
Resolve relative dates against now; if weekday and date-number disagree, trust the weekday.
CURRENT KNOWN EVENTS for <child>: <compact ledger>
[A document/image is attached at <path> — READ it; team sheets are usually PDFs/images.]
Caption/text: """<content>"""
Respond with ONLY: {"relevant": <bool>,
  "event": {iso, date, type, emoji, title, time, place, bib, team, notes, status}}
```

CODE then computes the key (`iso · group`), upserts into the ledger, decides new/changed/
unchanged, and renders the alert via `wa_events.event_lines()` — the digest renders the same way.

`claude -p` runs with `cwd=/tmp` deliberately — a neutral directory so it does **not**
load the project's `.mcp.json` (which would spawn the WhatsApp MCP server on every call
and add latency). Text extraction needs no tools; image extraction needs only `Read`.

---

## 5. Configuration (`~/.config/wa-alerts/config.json`)

| Key | Meaning |
|---|---|
| `bind`, `port`, `webhook_path` | Where the listener binds (`127.0.0.1:8769`) |
| `bridge_url` | Bridge REST base, `http://localhost:8085/api` |
| `bridge_token_path` | Reads the bearer token from `store/.bridge-token` (or set `bridge_token`) |
| `self_jid` | Recipient of alerts — `<your-number>@s.whatsapp.net` |
| `target_chats` | Group JIDs to watch (others ignored) |
| `target_names` | JID → friendly group name, shown in the alert header |
| `chat_kids` | JID → child's name (e.g. `…@g.us` → `Alex`); tags each alert and the prompt |
| `chat_ignore` | JID → free-text "treat as NOT relevant" rule for that group only (e.g. mute "extra players for Saturday?" asks). Honoured by both listener and digest |
| `alert_prefix` | Header prefix emoji, default `🔔` |
| `prefilter_keywords` | Text-only gate: if a message contains none of these, skip without calling the model. **Image/document attachments ignore this** and are always analysed |
| `claude_timeout` | Per-call timeout (s) |
| `claude_cwd` | Working dir for `claude -p` (`/tmp`) |

**Live-tunable** (no restart): `target_chats`, `target_names`, `chat_kids`,
`chat_ignore`, `alert_prefix`, `prefilter_keywords`. **Restart-required**: the prompt or any
`listener.py` change.

Example of watched groups → child:
- Example GAA U10 → **Alex** · Example FC Under 10s → **Alex**
- Example GAA U12 → **Sam** · Example FC U12 → **Sam**

---

## 6. systemd services

Both are **user** services; linger is enabled for the service user, so they run without an
active login and start on boot.

- `wa-bridge.service` — runs the prebuilt binary with `WorkingDirectory` =
  `whatsapp-bridge/` (so its relative `store/` paths resolve), `EnvironmentFile=.env`,
  `Restart=always`.
- `wa-alerts.service` — runs `listener.py` with `PATH` including `~/.npm-global/bin`
  (so `claude` is found) and `WA_ALERT_CONFIG` set; `After=wa-bridge.service`,
  `Restart=always`.

Manage (prefix once per shell: `export XDG_RUNTIME_DIR=/run/user/$(id -u)`):

```bash
systemctl --user status  wa-bridge wa-alerts
systemctl --user restart wa-alerts          # after editing prompt/code
journalctl --user -u wa-alerts -f           # watch alerts live
journalctl --user -u wa-bridge  -n 50       # bridge connection log
```

---

## 7. Bridge code patch (pairing-code login)

Headless boxes can't easily display a QR. The patch in `whatsapp-bridge/main.go` adds an
opt-in **pairing-code** path: when `WA_PAIR_PHONE=<international digits>` is set, the
bridge calls whatsmeow's `PairPhone(...)` and prints an 8-character code to type into
WhatsApp (*Linked Devices → Link a Device → Link with phone number instead*). No QR, no
screen, no camera.

Notes that cost time to discover (see also the gotchas below):
- The pairing **display name must be `Chrome (Linux)`** form — a real browser/OS — or
  WhatsApp's server rejects the request with `400 bad-request`.
- The phone number must be international digits only (no leading `0`, no `+`).
- It only matters for **first** login; once paired, the session persists in
  `store/whatsapp.db` and the service reconnects automatically.

---

## 8. Gotchas / decisions

- **Port 8080 is held by a root-owned process** on this VM; our bridge (uid 1000) can't
  bind it. Everything uses **8085** instead (`.env` `WHATSAPP_BRIDGE_PORT`,
  `WHATSAPP_API_URL`, `.mcp.json` env, config `bridge_url`).
- **No feedback loop:** `FORWARD_SELF=false` stops the bridge from webhooking the
  listener's *own* self-sent alerts back into itself.
- **Webhook timeout:** bridge gives the webhook 30 s; the listener ACKs instantly and
  does the slow work off-thread.
- **JSON, not sentinels:** an earlier "reply `SKIP` or the alert" contract broke because
  the model sometimes prefixed a preamble starting with the word "SKIP"; switched to a
  parsed JSON object.
- **Cost:** every message passing the keyword pre-filter spawns one `claude -p` run
  (subscription tokens). Tighten `prefilter_keywords` if the groups get chatty.

---

## 9. Testing

Inject a synthetic webhook (no real WhatsApp traffic needed):

```bash
curl -s -X POST http://localhost:8769/whatsapp/webhook \
  -H 'Content-Type: application/json' \
  -d '{"sender":"353800000000",
       "content":"Training tomorrow 6:30pm at the main pitch, bring shin guards. Match Sat 10am away to City.",
       "chatJID":"<group-jid>@g.us","isFromMe":false}'

journalctl --user -u wa-alerts -n 5   # expect: [alert] sent for <group-jid>@g.us
```

A `[skip]` line means the model judged it irrelevant (or the keyword pre-filter dropped
it); `[warn]` means the model returned unparseable output.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| No alerts at all | `systemctl --user status wa-bridge wa-alerts`; both `active`? |
| Alerts not delivered | `journalctl --user -u wa-bridge | grep /api/send` — look for `success=true` |
| Everything `[skip]` | Keyword pre-filter too tight, or genuinely no training/match content |
| `[warn] no JSON` | `claude -p` returned prose — check `claude -p "hi"` works for the service user |
| Bridge keeps restarting | `journalctl --user -u wa-bridge` — usually a port clash or logged-out device (re-pair) |
| Device logged out | WhatsApp drops linked devices after ~14 days if the phone never connects; re-run pairing |

---

## 11. Daily morning digest (`digest.py` + timer)

Alongside the realtime alerter, a per-child digest runs **twice daily (08:00 & 18:00
Europe/Dublin)** and sends each child a **"rest of this week"** summary — every
training/match from today **through the upcoming Sunday**. The window shrinks as the week
progresses and **resets each Monday** to the full Mon→Sun week (training is arranged weekly,
so a week-ahead view fits better than a fixed 2-day horizon).

Every child's digest uses one **fixed layout** (enforced in the prompt's `OUTPUT FORMAT`
block) so both boys read identically: a code-added title (`📅 This week — <kid> (through
Sun DD Mon)`), `**Weekday DD Month**` day headers, and one contiguous block per event
(`<emoji> title` then its `🕒/📍/🎽/👥/📝` lines, no blank line inside the block). No
preamble, no `---` rules, no sign-off; a single `⚠️` line only for a genuine clash.

Two behaviours keep it quiet:
- **Empty days suppressed, cancellations kept** — days no message mentions are omitted
  entirely (no "nothing on Friday" filler), but a **cancelled/postponed** session or match
  is treated as real news worth showing (a 🚫 line — "nothing's on, don't turn up"), not as
  an empty day to drop. The realtime listener likewise alerts cancellations.
- **No-change suppression** — each run passes the *previously sent* digest to `claude`,
  which replies `NO_CHANGE` (and nothing is sent) if the actionable plans are unchanged.
  Last-sent digests are stored per child under `~/.local/share/wa-alerts/state/`. This stops
  the 18:00 run from re-sending an unchanged 08:00 digest.

- **Script:** `~/.local/share/wa-alerts/digest.py` — reads the last `LOOKBACK` (default 10)
  days of messages for each child's groups (enough to catch the week's plans announced the
  prior weekend), builds a transcript, fetches attachments and team sheets, asks `claude -p`
  for a today→Sunday digest, and sends one message per child via `/api/send`.
  The Sunday end-of-window is computed in `Europe/Dublin` (`now + (6 - weekday)` days).
- **Schedule:** `wa-digest.timer` → `wa-digest.service` (oneshot). Two `OnCalendar` lines,
  `08:00:00` and `18:00:00 Europe/Dublin` (DST-safe), `Persistent=true` so a missed run
  (VM asleep) fires on wake.
- **Run manually:** `python3 ~/.local/share/wa-alerts/digest.py [lookback_days]`
- **Timer state:** `systemctl --user list-timers wa-digest.timer`

### Link / team-sheet handling

Bib and team-strip colours are frequently shared as **Google Sheets links**, not images.
`digest.py` extracts `docs.google.com/spreadsheets/d/<ID>` links and fetches each as CSV
via `/export?format=csv` (works for "anyone with link" sheets; auth-walled ones return
non-CSV and are skipped). The roster CSVs are passed to `claude`, which finds the child's
name to resolve **their** colour. Both `listener.py` and `digest.py` read image **and PDF/
document** attachments (team sheets are often PDFs); `digest.py` also adds the Sheets-link
path. Maps pins (`maps.app.goo.gl`) are not yet resolved.

### Known data limits

- **Media expiry:** WhatsApp purges media from its CDN after ~14 days, **and 403s on
  re-download far sooner** (often within minutes) — so `/api/download` is unreliable for any
  image the digest didn't see live. The realtime `listener.py` works around this by caching
  every incoming image (inline webhook bytes) to `~/.local/share/wa-alerts/media/`; the digest
  reads that cache first (`cached_image()`) and only falls back to `/api/download`. Images that
  arrived **before** this cache existed, or in history back-fill, still can't be recovered.
- **Name matching:** children are matched in rosters by the `chat_kids` first name. If a
  roster has duplicates or uses full names only, add the full/last name there to
  disambiguate; otherwise the digest says "check sheet".
