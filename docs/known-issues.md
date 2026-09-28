# Known issues

## `whatsapp-mcp-server` is pinned to mcp v1

**Status:** open — deliberately deferred.

`whatsapp-mcp-server/pyproject.toml` pins `mcp[cli]>=1.28.1,<2`. Without the `<2` bound the
build fails at import:

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer.
```

`main.py` still targets the v1 API (`from mcp.server.fastmcp import FastMCP`). The pin keeps CI
green; it does not fix the code.

**How it surfaced (2026-09-28).** CI had not run on `main` since 2026-06-26. mcp 2.x shipped in
between, so the first push after that window re-resolved the unbounded requirement and failed —
three months after the breakage actually occurred. `uv.lock` had also drifted to `mcp 1.27.1`,
*below* the declared floor, so the lockfile wasn't pinning anything usable either.

**To resolve:** migrate `main.py` to the 2.x API (`from mcp.server.mcpserver import MCPServer`,
plus the other v2 changes — see the
[migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver)),
then drop the `<2` bound.

**Priority:** low for the digest deployment, which runs only `whatsapp-bridge` and `alerter/` —
the MCP server is inherited from upstream and is not part of that pipeline. It matters only if
you want to drive WhatsApp from an MCP client.
