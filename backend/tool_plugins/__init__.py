"""Drop-in tool plugins.

Any `*.py` module placed in this package that uses the `@tool` decorator
from `backend.tool_sdk` is auto-discovered at startup by `tool_loader.py`
and registered into the live `_TOOLS` registry — no edits to `tools.py`,
no server restart needed (use the Tools page "Reload" button to re-scan).

This folder is a DEVELOPER / DEPLOY artifact: placing a file here runs
Python in the server process. The admin web UI must never accept uploaded
code — it only flips bindings (enable/disable + agent wiring).
"""
