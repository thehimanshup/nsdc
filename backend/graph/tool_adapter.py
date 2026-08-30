"""Tool-name (de)sanitisation + resolution for the graph engine.

OpenAI/LangChain tool names must match ^[a-zA-Z0-9_-]+$, so a registry id like
`records.create` is exposed to the model as `records__create`. This module
maps between the two and resolves a (possibly sanitised) name back to the real
`Tool`. The LangChain wrapping itself lives in `lc_tools.py`.
"""
from __future__ import annotations

from typing import Optional

from ..tools import get_tool, Tool

# OpenAI function names must match ^[a-zA-Z0-9_-]+$ — dots are illegal.
_SEP = "__"


def sanitize(tool_id: str) -> str:
    return tool_id.replace(".", _SEP)


def desanitize(name: str) -> str:
    return name.replace(_SEP, ".")


def resolve(sanitized_or_real: str) -> Optional[Tool]:
    """Resolve a (possibly sanitised) tool name back to a real Tool."""
    return get_tool(desanitize(sanitized_or_real)) or get_tool(sanitized_or_real)
