# Alerter & AI Digest

A self-hosted layer on top of the WhatsApp bridge that turns incoming messages into
**realtime alerts** and **scheduled digests** delivered to your own WhatsApp "message yourself"
chat. The bridge forwards incoming messages to a local webhook; these scripts filter them through
headless [`claude`](https://docs.anthropic.com/en/docs/claude-code) and push only what matters.

Two **independent** pipelines — run either or both:

| Pipeline | Scripts | What it does |
|---|---|---|
| **Realtime training alerter** | `listener.py`, `digest.py`, `wa_events.py` | Watches configured group chats, extracts parent-actionable events (training/matches: date, time, place, kit) into a per-child **event ledger**, and alerts on anything new or changed. `digest.py` renders the same ledger as a twice-daily "rest of this week" summary. Architecture: [`../docs/realtime-alerter.md`](../docs/realtime-alerter.md). |
| **Weekly AI digest** | `topics_digest.py`, `wa_events.py` | Curates high-signal items (releases, tools, how-tos, links, takeaways) out of noisy channels, deduped across runs, into one weekly digest. |

The model only ever **extracts** structured data; the code owns dedup, state and rendering, so the
two channels can't disagree and repeated messages don't re-alert.

## Prerequisites

- The WhatsApp **bridge** running with webhook forwarding enabled (`WEBHOOK_URL` pointing at this
  listener, e.g. `http://localhost:8769/whatsapp/webhook`). See the repo root README.
- The **`claude` CLI** on `PATH` and authenticated (used headlessly via `claude -p`).
- **Python 3.11+** (standard library only — no extra packages).
- Linux with **systemd user services** for scheduling (optional; you can also run the scripts by
  hand or from cron).

## Install

```bash
# 1. Scripts
mkdir -p ~/.local/share/wa-alerts
cp alerter/*.py ~/.local/share/wa-alerts/

# 2. Config — copy the template and fill in your JIDs / self_jid
mkdir -p ~/.config/wa-alerts
cp alerter/config.example.json ~/.config/wa-alerts/config.json
$EDITOR ~/.config/wa-alerts/config.json

# 3. systemd units (edit paths/timezone in the templates first if needed)
cp alerter/systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wa-alerts.service          # realtime listener
systemctl --user enable --now wa-digest.timer            # training digest 08:00 & 18:00
systemctl --user enable --now wa-topics-digest.timer     # AI digest, Mondays 08:00
```

Discover the JIDs to put in `config.json` with the MCP server's `list_chats` tool (group JIDs look
like `1203630…@g.us`; your own is `<number>@s.whatsapp.net`).

## Run by hand

```bash
cd ~/.local/share/wa-alerts
python3 topics_digest.py --days 30 --dry-run   # preview the AI digest, send nothing
python3 topics_digest.py                        # weekly run (sends; writes dedup state)
python3 digest.py                               # training digest (sends if changed)
```

## Configuration

All keys live in `~/.config/wa-alerts/config.json` (see `config.example.json` for the full set):

| Key | Purpose |
|---|---|
| `bridge_url`, `bridge_token_path` | Reach the bridge REST API (`/send`, `/download`) |
| `messages_db` | Absolute path to the bridge's `messages.db` (read directly). Override with `WA_MESSAGES_DB`. |
| `self_jid` | Your own JID — where every alert/digest is sent |
| `target_chats`, `target_names`, `chat_kids` | Realtime alerter: which groups to watch and the child each maps to |
| `chat_ignore` | Per-group free-text "always treat as not relevant" rule |
| `prefilter_keywords` | Cheap text gate before calling the model (attachments bypass it) |
| `topic_digest` | AI digest: `channels` to curate, `lookback_days`, dedup `seen_file`/`seen_ttl_days` |

Config is re-read on every webhook for the listener, so edits take effect live. State (event
ledgers, the digest dedup file, cached media) lives under `~/.local/share/wa-alerts/`.

> **Note:** this layer is intentionally generic — the "training" pipeline is just one configuration
> (groups → children → events). Point it at any groups and adapt the prompts in `listener.py` /
> `digest.py` for a different extraction task.
