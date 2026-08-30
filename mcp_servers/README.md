# `mcp_servers/` — external MCP tool servers

This folder holds **standalone MCP servers** you build and run as separate
processes. The gov-services backend connects to them over the network purely as
a *client* (it never imports this code). Each server can expose one or many MCP
tools, which then appear on the admin **Tools** page and can be wired to agents
just like built-in tools.

> **Why not name this folder `mcp`?** A top-level `mcp/` folder would shadow the
> installed `mcp` Python SDK package and break `import mcp`. Keep it
> `mcp_servers/` (or any name other than `mcp`).

---

## Servers in this folder

| File | Port | Tools |
|------|------|-------|
| `mandi_price_server.py` | 9002 | `mandi_price` — live daily crop/vegetable/fruit prices (Agmarknet) |

---

## 1. Mandi-price server

### API key (for LIVE prices)
The server auto-loads the project's `.env`. Put a free key there:
```
DATA_GOV_IN_API_KEY=your_key_here
```
Get one at https://data.gov.in → My Account → Generate API key. Without a key,
the server still runs but only the **MSP fallback** works (grains/pulses/
oilseeds — not most vegetables/fruits). A shared public **demo key** is built in
as a last resort, but it is heavily rate-limited (HTTP 429), so use your own.

### Run it (plain `python`, NOT the venv)
The repo's `.venv` is broken (points at a missing base Python); the app runs on
the base interpreter. From the project root:
```powershell
python mcp_servers/mandi_price_server.py
```
You should see:
```
Mandi-price MCP server on http://127.0.0.1:9002/mcp  (Ctrl+C to stop)
API key in use: 579b464d…801b  (source: .env / env var)
```
Every live call then prints a `[mandi_price] -> GET … <- HTTP 200, N record(s)`
line so you can watch the key being used.

If you see `Errno 10048 (address already in use)`, another instance is already
on :9002 — stop it first (`Get-NetTCPConnection -LocalPort 9002 -State Listen`
→ `Stop-Process -Id <PID>`).

### Connect it to the app
1. Admin **Tools** page → **MCP servers** → **+ Add server**.
2. name `mandi_prices`, url `http://127.0.0.1:9002/mcp` (the `/mcp` is required),
   transport `streamable_http` → **Save & connect** → shows **connected · 1 tool**.
3. Expand `mcp.mandi_prices.mandi_price`, tick **agriculture** (and **cmo**), Save.
   Optional keywords so it fires in chat/voice:
   `["mandi price","crop price","vegetable price","fruit price","bhav","rate of","price of"]`

### Test it
- Tools page → **Test** on `mcp.mandi_prices.mandi_price`:
  ```json
  {"commodity": "Onion", "state": "Tamil Nadu"}
  ```
- Chat (agriculture agent): "What's today's tomato price in Tamil Nadu?"

---

## Corporate-network notes (HCL)
This network does inline TLS inspection. Two things are required for any Python
HTTP client here (both already baked into the mandi server):
1. **truststore** — `import truststore; truststore.inject_into_ssl()` so the OS
   cert store (with the corporate CA) is used; otherwise `CERTIFICATE_VERIFY_FAILED`.
2. **Browser User-Agent + `Connection: close`** — the appliance stalls the default
   `python-httpx` User-Agent (request hangs to timeout). A browser UA gets through.

---

## Building your own MCP server here
Copy `mandi_price_server.py` as a template:
1. `mcp = FastMCP("my_tools", host="127.0.0.1", port=<unique_port>)`
2. Define each tool with `@mcp.tool()` and a clear docstring (the docstring is
   what the agent's LLM reads to decide when to call it).
3. End with `mcp.run(transport="streamable-http")`.
4. Run it, then add it on the admin Tools page at `http://127.0.0.1:<port>/mcp`.

Use a **different port** per server. Keep secrets (API keys, tokens) in `.env`
or environment variables, never hard-coded.
