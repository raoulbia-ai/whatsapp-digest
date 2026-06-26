# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Codex, etc.) working in this repository. `CLAUDE.md` just points here.

## Repository

- **Repo:** [`raoulbia-ai/whatsapp-digest`](https://github.com/raoulbia-ai/whatsapp-digest) (remote `origin`). It's a personal fork of [`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp) (via `verygoodplugins/whatsapp-mcp`, kept as the `upstream` remote).
- **Default branch:** `main`.
- **What's ours:** the WhatsApp Digest layer in [`alerter/`](alerter/) (scheduled AI digests + live-updating summaries) and the bridge tweaks it relies on. The MCP bridge + server underneath are inherited from upstream.

## Architecture (read first)

Three layers, one repo:

```
whatsapp-digest/
├── whatsapp-bridge/        # Go bridge — talks to WhatsApp Web via whatsmeow
│   ├── main.go             # REST API + event loop
│   ├── webhook.go          # Outgoing webhook for incoming messages
│   └── store/              # SQLite (whatsapp.db, messages.db) + media — gitignored
├── whatsapp-mcp-server/    # Python MCP server — exposes tools to AI clients
│   ├── main.py             # FastMCP tool definitions
│   ├── whatsapp.py         # DB queries + bridge HTTP client
│   └── audio.py            # FFmpeg helpers
└── alerter/                # The digest layer (this fork's addition)
```

Data flow: AI client → MCP server (Python) → reads SQLite directly **or** calls bridge REST (`http://localhost:8080/api/*` by default; configurable via `WHATSAPP_API_URL` / `WHATSAPP_BRIDGE_PORT`) → bridge (Go) → WhatsApp Web. The digest layer reads from the bridge's webhook + `messages.db` and calls an LLM (the `claude` CLI) to extract structured facts; see [docs/realtime-alerter.md](docs/realtime-alerter.md).

Two SQLite databases:

- `whatsapp.db` — owned by whatsmeow (sessions, contacts, LID map). Treat as opaque.
- `messages.db` — owned by the bridge (chats, messages). Schema is ours.

## Local commands

```bash
# Go bridge
cd whatsapp-bridge
go run .                    # dev
go build -o whatsapp-bridge && ./whatsapp-bridge   # release-ish
golangci-lint run           # lint
go test ./...               # tests (sparse today)

# Python MCP server
cd whatsapp-mcp-server
uv sync --extra dev
uv run main.py              # dev
uv run pytest -v            # tests
uv run ruff check .         # lint
uv run ruff format .        # format
```

## CI

`.github/workflows/ci.yml` runs on every push/PR: Python lint (`ruff`), Python tests (`pytest`), Go lint (`golangci-lint`), Go build. `security.yml` runs CodeQL + dependency scans. Keep these green.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `WHATSAPP_DB_PATH` | `../whatsapp-bridge/store/messages.db` | SQLite path used by the MCP server |
| `WHATSMEOW_DB_PATH` | `../whatsapp-bridge/store/whatsapp.db` | whatsmeow SQLite (LID ↔ phone resolution via `whatsmeow_lid_map`) |
| `WHATSAPP_API_URL` | `http://localhost:8080/api` | Bridge REST endpoint |
| `WHATSAPP_BRIDGE_PORT` | `8080` | Port the bridge binds to |
| `WHATSAPP_BRIDGE_TOKEN` | generated in `whatsapp-bridge/store/.bridge-token` | Bearer token required for bridge REST calls |
| `WHATSAPP_MEDIA_ROOTS` | `~/.local/share/whatsapp-mcp/outbox` | Path-list of directories allowed for outbound media files |
| `WEBHOOK_URL` | `http://localhost:8769/whatsapp/webhook` | Outgoing webhook for incoming messages (empty = disabled) |
| `FORWARD_SELF` | `true` | Whether self-sent messages are forwarded |

When adding a new env var: document it here, in `README.md`, and in `.env.example`.

## Gotchas (read before editing)

1. **JIDs.** WhatsApp identifies users as `1234567890@s.whatsapp.net` (DM), `123456@g.us` (group), and `<random>@lid` (link-ID, anonymous). The bridge maintains a phone↔LID map in `whatsapp.db.whatsmeow_lid_map`. Many "user is missing" / "messages don't show" bugs trace back to JID-form mismatches. Always think about both forms.
2. **Media files** live under `store/{chat_jid}/` with timestamp + message-ID filenames. Don't hand-construct these paths in client code; use the bridge's `/api/download` endpoint.
3. **Audio.** WhatsApp voice messages must be Opus `.ogg`. The MCP server's `send_audio_message` tool auto-converts via FFmpeg if installed.
4. **History sync** is controlled by the *primary* device (the phone). The bridge can request more (see the `--full-history-pair` flag), but the phone has the final word.
5. **`messages.db` is the source of truth for the MCP server.** Don't make read operations depend on the bridge being up.
6. **Outgoing calls are not visible to linked devices.** Don't promise features that depend on them.
7. **Digest reliability.** The LLM only *extracts* structured facts; the code owns memory, de-duplication, and rendering against a deterministically-keyed ledger. Keep that split — don't push state decisions into the prompt.

## Where to make changes

| You want to… | Touch this file |
|---|---|
| Add or modify an MCP tool | `whatsapp-mcp-server/main.py` |
| Change DB queries / data conversion | `whatsapp-mcp-server/whatsapp.py` |
| Change bridge REST API or event handling | `whatsapp-bridge/main.go` |
| Change webhook payload | `whatsapp-bridge/webhook.go` |
| Change digest extraction / rendering | `alerter/` (see its README) |
| Change CI behavior | `.github/workflows/*.yml` |

## Persona for AI agents working in this repo

- **Be terse.** Don't restate the question.
- **Be decisive.** Pick the smallest change that fixes the problem.
- **Bias to action** for low-risk improvements (lint, tests, error messages, comments that explain *why*).
- **Ask** before architectural changes or dependency additions.
- **Cite files with `path:line`** when discussing code.
