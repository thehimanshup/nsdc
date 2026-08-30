"""Agent config UI save-path — RAG + skills wiring + tools (IDs AG-*).

Covers what the redesigned tabbed agent modal persists via the admin API.

Run: .venv/Scripts/python.exe -m unittest tests.test_agent_config -v
"""
import os
import tempfile
import unittest

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cbtest_"))

from fastapi.testclient import TestClient

import backend.main as m
from backend import auth as A, skills


def _attach(c, skill_id, agent_id, on=True):
    row = [s for s in c.get("/api/v1/admin/skills").json()["skills"] if s["id"] == skill_id][0]
    agents = set(row.get("agents") or [])
    agents.add(agent_id) if on else agents.discard(agent_id)
    c.put(f"/api/v1/admin/skills/{skill_id}/binding",
          json={"enabled": True, "agents": list(agents)})


class TestAgentConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m.app.dependency_overrides[A.require_admin] = lambda: {"sub": "t"}
        cls.ctx = TestClient(m.app)
        cls.c = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        m.app.dependency_overrides.clear()

    def test_AG01_rag_fields_patch(self):
        r = self.c.patch("/api/v1/admin/agents/revenue",
                         json={"corpus_id": "revenue_kb", "cross_corpus_read": ["agriculture", "cmo"]})
        ag = r.json()["agent"]
        self.assertEqual(ag["corpus_id"], "revenue_kb")
        self.assertEqual(set(ag["cross_corpus_read"]), {"agriculture", "cmo"})

    def test_AG02_post_new_agent_cross_corpus(self):
        r = self.c.post("/api/v1/admin/agents", json={
            "id": "ag_new_test", "name": "AG New", "cross_corpus_read": ["health"]})
        self.assertEqual(r.status_code, 200)
        ag = self.c.get("/api/v1/admin/agents/ag_new_test").json()
        self.assertIn("health", ag["cross_corpus_read"])

    def test_AG03_04_attach_detach_skill(self):
        skills.save_skill(id="ag_skill", name="AG Skill", default_agents=[])
        _attach(self.c, "ag_skill", "revenue", on=True)
        self.assertTrue(any(s.id == "ag_skill" for s in skills.skills_for_agent("revenue")))
        _attach(self.c, "ag_skill", "revenue", on=False)   # AG-04 detach
        self.assertFalse(any(s.id == "ag_skill" for s in skills.skills_for_agent("revenue")))

    def test_AG05_tools_persist(self):
        r = self.c.patch("/api/v1/admin/agents/revenue",
                         json={"tool_ids": ["digilocker.fetch_patta", "digilocker.fetch_ec"]})
        self.assertEqual(set(r.json()["agent"]["tool_ids"]),
                         {"digilocker.fetch_patta", "digilocker.fetch_ec"})


if __name__ == "__main__":
    unittest.main()
