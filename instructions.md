# WhatsApp MCP — Personal Use Handover

## Overview

**Repo:** `verygoodplugins/whatsapp-mcp`
**Source:** https://github.com/verygoodplugins/whatsapp-mcp
**Origin:** Fork of `lharries/whatsapp-mcp`, currently the most actively maintained option for personal WhatsApp.

Two-component architecture:
- **Go bridge** (`whatsapp-bridge/`) — connects to WhatsApp via the whatsmeow multidevice API, stores all messages locally in SQLite
- **Python MCP server** (`whatsapp-mcp-server/`) — exposes MCP tools to Claude or any MCP client

All message data stays on-device. Nothing is sent to an LLM unless explicitly requested through a tool call.

---

## Prerequisites

| Dependency | Version |
|---|---|
| Go | 1.24+ |
| Python | 3.11+ |
| uv (Python package manager) | latest |
| FFmpeg | optional — required for voice message conversion |

MCP client: Claude Desktop, Claude Code, or Cursor.

---

## Installation

```bash
git clone https://github.com/verygoodplugins/whatsapp-mcp.git
cd whatsapp-mcp
```

### 1. Start the Go bridge

```bash
cd whatsapp-bridge
go run .
```

On first launch, a QR code is printed to the terminal. Scan it in WhatsApp:
**Settings → Linked Devices → Link a Device**

The bridge stores a local REST API bearer token at:
```
whatsapp-bridge/store/.bridge-token
```

Once authenticated, the bridge syncs messages into a local SQLite database and runs on `localhost` (default port `8080`).

### 2. Configure the MCP server

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/whatsapp-mcp/whatsapp-mcp-server",
        "run",
        "main.py"
      ]
    }
  }
}
```

Replace `/path/to/whatsapp-mcp` with your actual clone path.

---

## Available Tools

| Tool | Description |
|---|---|
| `search_messages` | Full-text search across all messages |
| `get_messages` | Retrieve messages from a specific chat |
| `list_chats` | List all chats with metadata |
| `get_chat` | Get metadata for a specific chat by JID |
| `find_dm_chat` | Find a direct message thread with a contact |
| `get_contact_chats` | All chats involving a contact |
| `get_last_interaction` | Last message exchanged with a contact |
| `get_message_context` | Messages surrounding a specific message |
| `search_contacts` | Search contacts by name or phone number |
| `send_message` | Send text to an individual or group |
| `send_file` | Send image, video, document, or voice note |
| `download_media` | Download media from a received message |

---

## Key Features vs Original (`lharries/whatsapp-mcp`)

- **Webhook support** — incoming messages can be forwarded to an external URL for event-driven integrations
- **Call history** — incoming voice/video calls captured into the SQLite database
- **Improved contact search** — returns contacts in `Name (phone)` format
- **Local REST API security** — bearer token auth on all `/api/*` routes; only accepts loopback `Host` headers, blocking access from other local processes
- **Media path confinement** — bridge only reads from configured `WHATSAPP_MEDIA_ROOTS`; defaults to `~/.local/share/whatsapp-mcp/outbox`

---

## Webhook Configuration (optional)

Set the `WHATSAPP_WEBHOOK_URL` environment variable before starting the bridge:

```bash
export WHATSAPP_WEBHOOK_URL=http://localhost:7860/webhook
cd whatsapp-bridge && go run .
```

The bridge will POST incoming message payloads to that URL as JSON. Useful for triggering downstream logic (e.g. Langflow flows, n8n, or custom HTTP listeners) without polling.

---

## Data & Privacy Notes

- All messages stored in local SQLite only
- Bridge token stored at `whatsapp-bridge/store/.bridge-token` — treat as a secret
- Media outbox defaults to `~/.local/share/whatsapp-mcp/outbox`
- No cloud sync, no third-party API keys required
- Subject to WhatsApp's [lethal trifecta](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) prompt injection risk — be cautious about what message content you expose to tool calls

---

## Restarting

The bridge does not need to re-authenticate after the first QR scan (session is persisted). To restart:

```bash
# Terminal 1 — bridge
cd whatsapp-bridge && go run .

# Terminal 2 — MCP server starts automatically when Claude Desktop launches
```

If Claude Desktop shows the MCP server as disconnected, fully quit and relaunch.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| QR code not appearing | Delete `whatsapp-bridge/store/` and restart bridge |
| MCP server not listed in Claude Desktop | Check path in config JSON; uv must be on `$PATH` |
| Messages not syncing | Ensure phone is online; bridge syncs on connection |
| Media send fails | Confirm file is inside a `WHATSAPP_MEDIA_ROOTS` directory |
| Voice message not sending | Install FFmpeg and ensure it's on `$PATH` |

---

## References

- Repo: https://github.com/verygoodplugins/whatsapp-mcp
- Original: https://github.com/lharries/whatsapp-mcp
- whatsmeow library: https://github.com/tulir/whatsmeow
- MCP spec: https://modelcontextprotocol.io
