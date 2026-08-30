"""Phase 6f — LangGraph / LangChain orchestration backbone.

This package implements the "Agent Orchestration (Brain)" + "Subagents (Team)"
layers of the Agentic Design Pattern as a real LangGraph `StateGraph`, while
reusing the rest of the application unchanged (tools, records, consent, audit,
RAG, personas, providers, voice).

It is OPT-IN and non-breaking: the legacy `orchestrator._run_agent_turn` path
stays the default. Set `ORCHESTRATOR_ENGINE=graph` to route single-agent turns
through this graph instead. The HTTP/WS contract and all UIs are unchanged.
"""
from __future__ import annotations

# Lazily importable — the rest of the app must import fine even if langgraph
# isn't installed (legacy engine has no dependency on it).
try:
    import langgraph  # noqa: F401
    LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    LANGGRAPH_AVAILABLE = False

__all__ = ["LANGGRAPH_AVAILABLE"]
