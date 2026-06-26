# Security Policy

This is a personal, self-hosted project. Everything runs on the user's own
machine and no data leaves it.

## Reporting a vulnerability

Please report security issues **privately** — don't open a public issue, PR, or
discussion. Use GitHub's [private vulnerability reporting](https://github.com/raoulbia-ai/whatsapp-digest/security/advisories/new)
on this repository.

When reporting, include where possible: a description and its impact, the
affected version/commit, and steps to reproduce.

## Scope

The threat model assumes the human user of the host is trusted, but does **not**
assume every process on that host is. Issues that let a local caller abuse the
bridge's REST surface, escape the configured media roots, or exfiltrate message
data are in scope.

Out of scope: WhatsApp itself, the WhatsApp Web protocol, and `whatsmeow`
upstream (report those upstream); denial of service via request volume; and
anything requiring root/admin compromise of the host.

## Note

As with many MCP servers, this is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) —
prompt injection could lead to private data exfiltration. Be mindful of what
message content you expose to tool calls.
