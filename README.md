# WhatsApp Digest

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Go 1.25+](https://img.shields.io/badge/go-1.25+-00ADD8.svg)](https://go.dev/)

**Too many WhatsApp groups. Updates landing across every channel, all day. Endlessly scrolling back through threads and hopping between chats so you don't miss the one message that actually mattered — that's WhatsApp burnout. This is our answer.**

WhatsApp Digest collapses all your noisy groups into **one channel with the key information only** — your own "message yourself" chat. An LLM reads every group and produces **scheduled digests that combine what matters across all your channels** into a single summary, then **keeps those summaries current in real time** — revising an entry in place as plans change, instead of leaving you to reconcile a dozen threads yourself. It's self-hosted: everything runs on your own machine and your messages never leave it.

## How it works

Two pipelines ship as a self-contained package in [`alerter/`](alerter/):

- **Scheduled AI digests** that merge the key information from all your groups into one summary — grouped, deduped, high-signal, no chatter.
- **Live-updating summaries** — as new messages land, an LLM updates the shared ledger behind the digest, so a changed time / venue / plan revises the existing entry (flagged `(updated)`) instead of adding noise.

The design decision that makes it reliable: the LLM **only extracts structured facts** from messages; the **code** owns memory, de-duplication, and rendering. Every event is one deterministically-keyed record in a ledger, so the digest renders the same way every time instead of drifting. Architecture in [docs/realtime-alerter.md](docs/realtime-alerter.md).

Under the hood the digest layer reads from a WhatsApp [MCP](https://modelcontextprotocol.io/) bridge — a Go service linked to WhatsApp as a companion device, plus a Python MCP server. The bridge also adds document attachments in webhook forwarding and headless **pairing-code login** (`WA_PAIR_PHONE`) for servers with no scannable QR.

**Start here:** the [`alerter/` README](alerter/README.md) covers digest setup. The rest of this document documents the underlying bridge + MCP server the digest runs on.

## Features

**The digest — what this fork adds:**

- **Cross-channel digests** — one scheduled summary that merges the key info from all your groups: grouped, deduped, chatter stripped out.
- **Live-updating summaries** — a changed time / venue / plan revises the existing entry in place (flagged `(updated)`) instead of piling on new pings.
- **Realtime alerts** — the moment a message that matters lands, a concise note is pushed to your own "message yourself" chat.
- **Structured extraction** — an LLM reads text, images, and PDF attachments (e.g. team sheets) and returns structured facts; the code owns the ledger, so results stay deterministic.
- **Self-hosted, no API key required** — runs headless on your own machine via systemd timers + the `claude` CLI; your messages never leave it.

**The bridge underneath (inherited from upstream):**

- Local WhatsApp link via whatsmeow (companion device); all messages stored in local SQLite.
- Webhook forwarding of incoming messages — text, images, and documents.
- Headless pairing-code login (`WA_PAIR_PHONE`) for servers with no scannable QR.

## Installation

This sets up the **bridge** (which links your WhatsApp) and points you at the **digest** (the product). Both run headless on a machine you control — a VM, home server, or Pi. Claude Desktop / Cursor are **optional** and only needed if you also want to query your WhatsApp interactively from an MCP client; the digest itself does not use them.

### Prerequisites

- Go 1.25+
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- FFmpeg (optional, for voice message conversion)
- Claude Desktop or Cursor (optional — interactive use only)

### Quick Start

1. **Clone the repository**

   ```bash
   git clone https://github.com/raoulbia-ai/whatsapp-digest.git
   cd whatsapp-digest
   ```

2. **Start the WhatsApp bridge**

   ```bash
   cd whatsapp-bridge
   go run .
   ```

   On first start, the bridge prints and stores a local REST API token at
   `whatsapp-bridge/store/.bridge-token`. Scan the QR code with WhatsApp on
   your phone to authenticate. On a headless server with no scannable QR, use
   pairing-code login via `WA_PAIR_PHONE`.

3. **Set up the digest** — this is the product.

   Follow the [`alerter/` README](alerter/README.md) to configure scheduled
   digests and live-updating summaries. It runs headless (systemd timers + the
   `claude` CLI); no desktop app is involved.

## Configuration

Copy `.env.example` to `.env` and configure as needed:

| Variable               | Default                                  | Description                                  |
| ---------------------- | ---------------------------------------- | -------------------------------------------- |
| `WHATSAPP_BRIDGE_PORT` | `8080`                                   | Port for Go bridge REST API                  |
| `WEBHOOK_URL`          | `http://localhost:8769/whatsapp/webhook` | Webhook for incoming messages                |
| `FORWARD_SELF`         | `true`                                   | Forward messages sent by self                |
| `WHATSAPP_DB_PATH`     | `../whatsapp-bridge/store/messages.db`   | Path to SQLite database                      |
| `WHATSMEOW_DB_PATH`    | `../whatsapp-bridge/store/whatsapp.db`   | whatsmeow DB used for LID ↔ phone resolution |
| `WHATSAPP_API_URL`     | `http://localhost:8080/api`              | Go bridge REST API URL                       |
| `WHATSAPP_BRIDGE_TOKEN` | generated in `whatsapp-bridge/store/.bridge-token` | Bearer token required for bridge REST calls |

### Bridge authentication

The bridge requires bearer-token authentication for every `/api/*` request and
accepts only exact loopback Host headers for its configured port. This protects
the local REST API from other local processes and browser DNS-rebinding attacks.

On first start, the bridge generates a 256-bit token, writes it to
`whatsapp-bridge/store/.bridge-token` with owner-only permissions, and prints a
setup banner. Readers (the digest pipeline / MCP server) use `WHATSAPP_BRIDGE_TOKEN`
first, then fall back to that token file. For split deployments, containers, or
process managers that do not share the repository directory, set the same
`WHATSAPP_BRIDGE_TOKEN` value for both the bridge and its readers.

### CLI flags (Go bridge)

| Flag                  | Default | Description                                                                                                                                                                                                                                                       |
| --------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--full-history-pair` | `false` | Request full history at pair time. Only takes effect on a fresh pair (no existing `whatsapp.db`); no-op for already-paired sessions. The phone ultimately decides the actual history window sent — see [Requesting full history](#requesting-full-history) below. |

### Requesting full history

whatsmeow's default pairing asks for "recent sync" — roughly the last 3 months, with the exact window decided by the phone. If you want to pull more history at pair time:

```bash
# Stop the bridge
launchctl bootout gui/$UID/com.whatsapp-mcp.bridge    # or however you manage it

# Back up, then remove the auth session (keeps messages.db intact)
cp whatsapp-bridge/store/whatsapp.db{,.bak}
rm whatsapp-bridge/store/whatsapp.db

# Re-pair with the flag
cd whatsapp-bridge
./whatsapp-bridge --full-history-pair
# Scan the QR with WhatsApp → Settings → Linked Devices → Link a Device
# Wait for "History sync complete" in the logs (can take 10-30 minutes)
# Ctrl+C when sync has quiesced, then restart under your normal process manager
```

Caveats:

- **The phone decides the actual cap.** The flag requests up to 10 years / 100 GB, but WhatsApp's iOS primary device enforces its own retention policy. iPad companion is documented at ~1 year max; other linked devices appear to follow similar logic.
- **Only effective on a fresh pair.** With `whatsapp.db` already present, no new pair handshake fires and the flag is a no-op.
- **Messages the phone has deleted are not recoverable** — auto-expire, low-storage cleanup, and manual delete all leave no trace for the phone to share.

## Development

### Running Tests

```bash
cd whatsapp-mcp-server
uv pip install -e ".[dev]"
uv run pytest -v
```

### Linting

```bash
# Python
cd whatsapp-mcp-server
uv run ruff check .
uv run ruff format .

# Go
cd whatsapp-bridge
golangci-lint run
```

### Building

```bash
# Go bridge
cd whatsapp-bridge
go build -o whatsapp-bridge

# Run the binary
./whatsapp-bridge

# During development (avoids stale binaries)
go run .
```

## Troubleshooting

### Authentication Issues

- **QR Code Not Displaying**: Restart the bridge. Check terminal QR code support.
- **Device Limit Reached**: Remove a linked device from WhatsApp Settings > Linked Devices.
- **No Messages Loading**: Initial sync can take several minutes for large chat histories.
- **Out of Sync**: Back up `whatsapp-bridge/store`, then move
  `whatsapp-bridge/store/whatsapp.db` aside and re-authenticate. Keep
  `messages.db` unless you intentionally want to discard local message history.
- **Bridge returns 401 Unauthorized**: Restart the bridge so it creates
  `whatsapp-bridge/store/.bridge-token`, then restart the MCP server. If the MCP
  server cannot read that file, set `WHATSAPP_BRIDGE_TOKEN` to the same value in
  both environments.
- **Bridge returns 403 Forbidden for Host**: Use `WHATSAPP_API_URL` with
  `http://127.0.0.1:<port>/api`, `http://localhost:<port>/api`, or
  `http://[::1]:<port>/api`; custom hostnames and missing ports are rejected.

### App State / LTHash Conflicts

Some WhatsApp account state is managed by whatsmeow in
`whatsapp-bridge/store/whatsapp.db`. If the bridge reports errors like:

```text
SendAppState failed: server returned error updating app state (regular_low):
<error code="409" text="conflict"/>
failed to verify patch v12345: mismatching LTHash
```

then WhatsApp's app-state patch chain for the linked device is out of sync.
This usually affects operations that write chat settings such as archive,
mute, or pin state. Incoming and outgoing messages may still work because
message storage lives separately in `messages.db`.

Known manual resync attempts such as `FetchAppState(..., fullSync=true)` may
still fail on this upstream app-state error class. The practical recovery path
is to reset the whatsmeow session and re-pair:

```bash
# Stop the bridge first.
launchctl bootout gui/$UID/com.whatsapp-mcp.bridge    # or however you manage it

# Back up the whole runtime store.
cp -a whatsapp-bridge/store whatsapp-bridge/store.bak.$(date +%Y%m%d%H%M%S)

# Reset only the whatsmeow session/app-state DB.
mv whatsapp-bridge/store/whatsapp.db whatsapp-bridge/store/whatsapp.db.lthash.bak

# Restart the bridge and scan the new QR code.
cd whatsapp-bridge
./whatsapp-bridge    # or `go run .` during development
```

Do not remove `whatsapp-bridge/store/messages.db` for this recovery unless you
also want to delete the local message archive.

### Windows

Windows requires CGO for go-sqlite3. Install [MSYS2](https://www.msys2.org/) and enable CGO:

```bash
go env -w CGO_ENABLED=1
go run .
```

## Security Notice

> **Caution**: As with many MCP servers, this is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). Prompt injection could lead to private data exfiltration. Use with awareness.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Credits

The WhatsApp Digest layer (`alerter/`) and the bridge changes it relies on are maintained here. The underlying MCP bridge + server are a fork of [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) by [Luke Harries](https://github.com/lharries) (via [verygoodplugins/whatsapp-mcp](https://github.com/verygoodplugins/whatsapp-mcp)) — full credit to those authors. MIT-licensed.

## Links

- [MCP Specification](https://modelcontextprotocol.io/)
- [whatsmeow](https://github.com/tulir/whatsmeow) - WhatsApp Web API library for Go
- [FastMCP](https://github.com/jlowin/fastmcp) - Fast Model Context Protocol implementation
