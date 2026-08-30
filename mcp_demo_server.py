"""Example EXTERNAL MCP server — run this in its own terminal.

This stands in for a third-party tool server (a state land-records API, a
payments gateway, etc.) that lives on a *different* machine and is owned by
someone else. Our backend connects to it purely as a client over the network;
it never imports or hosts this code.

Run it:

    python mcp_demo_server.py

It serves the Model Context Protocol over streamable-HTTP at
    http://127.0.0.1:9001/mcp

Then point the backend at it by editing data/mcp_servers.json (see the
instructions in the chat) and clicking "Reload" on the admin Tools page.

Requires the `mcp` SDK (already installed):  pip install mcp
"""
from mcp.server.fastmcp import FastMCP

# host/port chosen to NOT clash with the backend (which runs on :8000).
mcp = FastMCP("demo_gov_tools", host="127.0.0.1", port=9001)


@mcp.tool()
def land_record_status(survey_no: str) -> dict:
    """Look up the mutation (name-transfer) status of a land record by survey
    number. Returns a deterministic mock so the demo is repeatable."""
    digits = "".join(ch for ch in survey_no if ch.isdigit()) or "0"
    pending = int(digits[-1]) % 2 == 0
    return {
        "ok": True,
        "survey_no": survey_no,
        "mutation_status": "PENDING" if pending else "COMPLETED",
        "office": "Sub-Registrar, Mylapore",
        "last_updated": "2026-06-10",
        "source": "demo MCP server (external)",
    }


@mcp.tool()
def change_text_case(text: str, to_case: str = "upper") -> dict:
    """Change the letter case of some text. Use this whenever the citizen asks
    to capitalize, uppercase, lowercase, or change the case of a word, name or
    any text. `to_case` is "upper" (capitalize / UPPERCASE) or "lower".
    """
    mode = (to_case or "upper").strip().lower()
    result = text.lower() if mode.startswith("low") else text.upper()
    applied = "lower" if mode.startswith("low") else "upper"
    return {
        "ok": True,
        "original": text,
        "to_case": applied,
        "result": result,
        "message": (f"Hurray! You used the MCP tool 🎉 — '{text}' in {applied}case "
                    f"is '{result}'."),
        "source": "demo MCP server (external)",
    }


@mcp.tool()
def ifsc_lookup(ifsc: str) -> dict:
    """Resolve a bank IFSC code to a branch (mock)."""
    return {
        "ok": True,
        "ifsc": ifsc.upper(),
        "bank": "State Bank of India",
        "branch": "Chennai Main",
        "city": "Chennai",
        "source": "demo MCP server (external)",
    }


if __name__ == "__main__":
    print("Demo MCP server listening on http://127.0.0.1:9001/mcp  (Ctrl+C to stop)")
    mcp.run(transport="streamable-http")
