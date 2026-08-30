"""Phase 7 — LangChain/LangGraph alignment + Skills (Milestones 1-5).

Consolidated, dependency-light regression suite. Runs without network and
without Pillow (the real `backend.orchestrator` imports `vision` -> `PIL`, which
isn't needed here), by stubbing `backend.orchestrator` with the two functions
the graph tool path calls. This stub must be installed BEFORE importing the
graph package, so it lives at module import time.

Run:
    .venv/Scripts/python.exe -m unittest tests.test_phase7_langchain_skills -v
"""
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("LLM_PROVIDER", "mock")


async def _fake_execute(citizen_id, conv_id, tool, *, args, channel, return_result=False):
    return {"ok": True, "record_id": "GRV-TN-2026-000999", "echo": args}


async def _fake_consent(*a, **k):
    return None


# Ensure `backend.orchestrator` is importable. In this dev env the real module
# imports `vision` -> `PIL` (absent), so fall back to a stub exposing the two
# functions the graph tool path calls. In a full env the real module imports
# fine and we leave it alone — M2 patches the two functions per-test either way,
# so this never leaks into other suites.
try:
    import backend.orchestrator  # noqa: F401
except Exception:
    _stub = types.ModuleType("backend.orchestrator")
    _stub._execute_tool_and_append = _fake_execute
    _stub._send_consent_request = _fake_consent
    sys.modules["backend.orchestrator"] = _stub

import backend.orchestrator as _orch_mod  # noqa: E402  (real or stub)

from backend import crypto_utils as _crypto  # noqa: E402
_crypto.init_keys()  # the app does this at startup; needed for audit signing

from langchain_core.tools import StructuredTool  # noqa: E402

from backend import routes_admin as ra  # noqa: E402
from backend import skills as skills_mod  # noqa: E402
from backend import skill_bindings as skill_bindings_mod  # noqa: E402
from backend import tools as tools_mod  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.graph import build, lc_tools, tool_adapter  # noqa: E402


def _echo_tool(tool_id="test.echo", agents=None, consent=False):
    return tools_mod.Tool(
        id=tool_id, name="Echo", description="echo back", connector="test",
        requires_consent=consent, consent_scope=("TEST" if consent else ""),
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        allowed_agents=list(agents or []),
        execute=lambda args, citizen_id: {"ok": True},
    )


class M1KeystoneAdapter(unittest.IsolatedAsyncioTestCase):
    """Milestone 1 — registry Tool -> LangChain StructuredTool + binding."""

    async def test_tools_wrap_bind_and_resolve(self):
        tools = lc_tools.langchain_tools_for_agent("cmo")
        self.assertTrue(tools, "no tools wired for cmo")
        self.assertTrue(all(isinstance(t, StructuredTool) for t in tools))
        self.assertTrue(all(isinstance(t.args_schema, dict) for t in tools))

        from backend.graph import llm_adapter
        model = llm_adapter.build_chat_model("cmo", channel="simulator")
        self.assertEqual(model._llm_type, "mock-tool-calling")
        bound = model.bind_tools(tools)
        self.assertEqual(sorted(bound.bound_names), sorted(t.name for t in tools))

        name = tools[0].name
        real = tool_adapter.resolve(name)
        self.assertIsNotNone(real)
        self.assertEqual(real.id, name.replace("__", "."))


class M2ConsentAndTools(unittest.IsolatedAsyncioTestCase):
    """Milestone 2 — dedicated consent node + StructuredTool execution."""

    async def asyncSetUp(self):
        # Patch the executor + consent fns on the orchestrator module (real or
        # stub) for the duration of each test, so execution never touches the
        # real store/dispatch and isolation is preserved across suites.
        for name, fn in (("_execute_tool_and_append", _fake_execute),
                         ("_send_consent_request", _fake_consent)):
            p = mock.patch.object(_orch_mod, name, fn, create=True)
            p.start()
            self.addCleanup(p.stop)

    async def test_graph_compiles_with_consent_topology(self):
        graph = build.get_graph()
        nodes = set(graph.get_graph().nodes)
        self.assertTrue({"consent", "tools", "agent"} <= nodes)

    async def test_node_tools_executes_and_captures_record_id(self):
        tools_mod.register(_echo_tool("test.echo", agents=["testagent"]))
        state = {
            "citizen_id": "ctz", "agent_id": "testagent", "conv_id": "c1",
            "channel": "simulator", "latest_user_text": "hi",
            "tool_calls": [{"name": "test__echo", "args": {"q": "hi"},
                            "id": "call_1", "type": "tool_call"}],
            "messages": [],
        }
        out = await build.node_tools(state)
        self.assertEqual(len(out["messages"]), 1)
        self.assertIn("GRV-TN-2026-000999", out["messages"][0].content)
        self.assertEqual(state.get("last_record_id"), "GRV-TN-2026-000999")
        self.assertEqual(out["tool_calls"], [])

    async def test_node_consent_pauses_for_consent_tool(self):
        tools_mod.register(_echo_tool("test.secret", agents=["testagent"], consent=True))
        base = {"citizen_id": "ctz", "agent_id": "testagent", "conv_id": "c1",
                "channel": "simulator", "latest_user_text": "hi"}
        paused = await build.node_consent(
            {**base, "tool_calls": [{"name": "test__secret", "args": {},
                                     "id": "c2", "type": "tool_call"}]})
        self.assertTrue(paused.get("consent_pending"))
        proceed = await build.node_consent(
            {**base, "tool_calls": [{"name": "test__echo", "args": {"q": "x"},
                                     "id": "c3", "type": "tool_call"}]})
        self.assertFalse(proceed.get("consent_pending"))


class M3SkillsCore(unittest.TestCase):
    """Milestone 3 — skills registry, wiring, tools-union, master switch."""

    def test_loader_reads_shipped_examples(self):
        n = skills_mod.load()
        self.assertGreaterEqual(n, 1)
        self.assertIsNotNone(skills_mod.get_skill("land_dispute"))

    def test_skill_brings_tool_and_respects_master_switch(self):
        tools_mod.register(_echo_tool("test.echo", agents=[]))  # not directly wired
        skills_mod.register_skill(skills_mod.Skill(
            id="t_skill", name="Test Skill", description="d",
            instructions="Always echo first.", tool_ids=["test.echo"],
            corpus_id="tcorpus", default_agents=["tagent"]))

        self.assertEqual([s.id for s in skills_mod.skills_for_agent("tagent")], ["t_skill"])
        self.assertEqual(skills_mod.skills_for_agent("other"), [])

        names = [t.name for t in lc_tools.langchain_tools_for_agent("tagent")]
        self.assertIn("test__echo", names)
        self.assertEqual(names.count("test__echo"), 1)

        settings.skills_enabled = False
        try:
            self.assertEqual(skills_mod.skills_for_agent("tagent"), [])
            self.assertNotIn("test__echo",
                             [t.name for t in lc_tools.langchain_tools_for_agent("tagent")])
        finally:
            settings.skills_enabled = True


class M4AdminApi(unittest.IsolatedAsyncioTestCase):
    """Milestone 4 — Skills admin CRUD + bindings (against live data/, cleaned up)."""

    SID = "phase7_test_skill"

    async def asyncTearDown(self):
        try:
            skills_mod.delete_skill(self.SID)
            skill_bindings_mod.delete_binding(self.SID)
        except Exception:
            pass

    async def test_create_wire_effective_delete(self):
        row = await ra.admin_save_skill(ra.SkillUpsertRequest(
            id=self.SID, name="Phase7 Test", description="temp",
            instructions="Do the test thing.",
            tool_ids=["digilocker.fetch_patta"], corpus_id="revenue",
            default_agents=[]))
        self.assertTrue(row["ok"])
        self.assertEqual(row["skill"]["missing_tools"], [])

        listed = await ra.admin_list_skills()
        self.assertTrue(any(s["id"] == self.SID for s in listed["skills"]))

        bind = await ra.admin_set_skill_binding(
            self.SID, ra.SkillBindingRequest(enabled=True, agents=["cmo"]))
        self.assertEqual(bind["binding"]["agents"], ["cmo"])
        self.assertIn(self.SID, [s.id for s in skills_mod.skills_for_agent("cmo")])

        with self.assertRaises(ra.HTTPException) as ctx:
            await ra.admin_set_skill_binding(
                self.SID, ra.SkillBindingRequest(enabled=True, agents=["nope_agent"]))
        self.assertEqual(ctx.exception.status_code, 400)

        deleted = await ra.admin_delete_skill(self.SID)
        self.assertTrue(deleted["ok"])
        self.assertIsNone(skills_mod.get_skill(self.SID))


class M5McpAsSkill(unittest.TestCase):
    """Milestone 5 — an MCP tool flows through a skill to an agent."""

    MCP_TOOL_ID = "mcp.land_records.fetch_rtc"
    SANITISED = "mcp__land_records__fetch_rtc"

    def test_mcp_tool_surfaces_via_skill(self):
        skills_mod.load()
        demo = skills_mod.get_skill("mcp_land_records")
        self.assertIsNotNone(demo, "shipped MCP demo skill missing")
        # before the server is connected the tool is flagged missing
        self.assertIn(self.MCP_TOOL_ID, ra._skill_row(demo)["missing_tools"])

        # simulate the wrapper tool mcp_loader.connect_all() would register
        tools_mod.register(tools_mod.Tool(
            id=self.MCP_TOOL_ID, name="Fetch RTC", description="remote lookup",
            connector="mcp:land_records", requires_consent=False, consent_scope="",
            input_schema={"type": "object",
                          "properties": {"survey_no": {"type": "string"}},
                          "required": ["survey_no"]},
            allowed_agents=[], source="mcp",
            execute=lambda args, citizen_id: {"ok": True, "rtc": "mock"}))
        self.assertEqual(ra._skill_row(demo)["missing_tools"], [])

        skills_mod.register_skill(skills_mod.Skill(
            id="m5_mcp_skill", name="M5 MCP", description="d",
            instructions="Use the land-records lookup.",
            tool_ids=[self.MCP_TOOL_ID], default_agents=["revenue"]))
        names = [t.name for t in lc_tools.langchain_tools_for_agent("revenue")]
        self.assertIn(self.SANITISED, names)

        resolved = tool_adapter.resolve(self.SANITISED)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, self.MCP_TOOL_ID)


class ScopeGuard(unittest.IsolatedAsyncioTestCase):
    """Topical-scope guardrail — deterministic block/allow + mock-mode check."""

    def test_deterministic_blocks_offtopic(self):
        from backend import scope_guard as sg
        for text, cat in [("tell me a joke", "joke"),
                          ("solve this puzzle", "riddle"),
                          ("write python code to sort a list", "code"),
                          ("pretend you are a waiter and take my order", "roleplay")]:
            v = sg.deterministic(text)
            self.assertIsNotNone(v, text)
            self.assertFalse(v.in_scope, text)
            self.assertEqual(v.category, cat, text)

    def test_deterministic_allows_onscope(self):
        from backend import scope_guard as sg
        for text in ["hi", "namaste", "I want to apply for the PM-KISAN scheme",
                     "what is my ration card status", "my water supply has a leak"]:
            v = sg.deterministic(text)
            self.assertIsNotNone(v, text)
            self.assertTrue(v.in_scope, text)

    async def test_check_blocks_and_allows_in_mock(self):
        from backend import scope_guard as sg
        blocked = await sg.check("tell me a joke", agent_id="cmo")
        self.assertFalse(blocked.in_scope)
        allowed = await sg.check("I need my patta from the revenue office", agent_id="revenue")
        self.assertTrue(allowed.in_scope)
        # undecided + mock => fail-open allow
        undecided = await sg.check("hmm yeah the thing about that", agent_id="cmo")
        self.assertTrue(undecided.in_scope)

    async def test_refusal_is_localized_english_template(self):
        from backend import scope_guard as sg
        text = await sg.refusal(agent_id="cmo", lang="en-IN", category="joke")
        self.assertTrue(text)
        self.assertIn("Chief Minister", text)


if __name__ == "__main__":
    unittest.main()
