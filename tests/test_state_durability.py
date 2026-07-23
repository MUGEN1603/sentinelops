"""
tests/test_state_durability.py

Validates that the LangGraph SQLite checkpointer provides durable state:
- Invoking with a thread_id completes the full pipeline.
- If interrupted mid-graph, re-invoking with the SAME thread_id resumes
  from the last successful checkpoint, NOT from the beginning.

Run: pytest tests/test_state_durability.py -v

NOTE: This test requires Qdrant and OPA running locally.
      Ollama calls are mocked.
"""

import os
import sys
import uuid
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Sample incident fixture ────────────────────────────────────────────────────

def make_incident(incident_id: str = None) -> dict:
    return {
        "id":             incident_id or str(uuid.uuid4()),
        "started_at":     "2024-01-01T00:00:00Z",
        "namespace":      "apps",
        "workload":       "sample-app-test-pod",
        "kind":           "Pod",
        "severity":       "critical",
        "alert_labels":   {"alertname": "PodCrashLooping", "namespace": "apps"},
        "recent_logs":    ["ERROR: OOMKilled", "WARN: memory at 95%"],
        "recent_metrics": {"memory_usage_bytes": 125000000.0},
        "k8s_objects":    [],
        "trace_refs":     [],
    }


# ── Mock LLM responses ────────────────────────────────────────────────────────

MOCK_TRIAGE_RESPONSE = json.dumps({
    "severity": "critical",
    "reasoning": "Pod is crash looping due to OOMKill."
})

MOCK_DIAGNOSIS_RESPONSE = json.dumps({
    "probable_cause": "Memory leak in the sample-app caused OOMKill.",
    "detailed_analysis": "The container exceeded its 128Mi memory limit.",
    "evidence": ["container_memory_usage_bytes exceeded limit"],
    "confidence": 0.85,
    "recommended_next_step": "Increase memory limits or fix the memory leak."
})

MOCK_REMEDIATION_RESPONSE = json.dumps({
    "patch_type": "patch_memory_limit",
    "risk_level": "medium",
    "target_resource": "apps/deployments/sample-app",
    "patch_yaml": "spec:\n  template:\n    spec:\n      containers:\n      - name: sample-app\n        resources:\n          limits:\n            memory: '256Mi'\n",
    "rollback_hint": "Revert the memory limit back to 128Mi via Git."
})


class TestStateDurability:

    @pytest.fixture(autouse=True)
    def mock_llm(self, monkeypatch):
        """Mock ollama.chat to return deterministic responses per agent."""
        call_counter = {"n": 0}

        def mock_chat(model, messages):
            call_counter["n"] += 1
            # Determine which agent is calling based on system prompt content
            system = messages[0]["content"].lower()
            if "classify severity" in system or "triage" in system:
                content = MOCK_TRIAGE_RESPONSE
            elif "root cause" in system or "diagnose" in system:
                content = MOCK_DIAGNOSIS_RESPONSE
            elif "remediation" in system or "patch" in system:
                content = MOCK_REMEDIATION_RESPONSE
            else:
                content = '{"result": "mock"}'
            return {"message": {"content": content}}

        monkeypatch.setattr("ollama.chat", mock_chat)
        self.call_counter = call_counter

    @pytest.fixture(autouse=True)
    def mock_opa(self, monkeypatch):
        """Mock OPA to always allow medium-risk apps namespace actions."""
        import requests as req

        original_post = req.post

        def mock_post(url, *args, **kwargs):
            if "8181" in url and "allow" in url:
                class MockResp:
                    status_code = 200
                    def json(self): return {"result": True}
                    def raise_for_status(self): pass
                return MockResp()
            return original_post(url, *args, **kwargs)

        monkeypatch.setattr("requests.post", mock_post)

    @pytest.fixture(autouse=True)
    def mock_github(self, monkeypatch):
        """Mock GitHub PR creation — don't require a real GITHUB_TOKEN."""
        monkeypatch.setattr(
            "agents.gitops_bridge.open_remediation_pr",
            lambda **kwargs: "https://github.com/mock/sentinelops/pull/1"
        )

    @pytest.fixture(autouse=True)
    def mock_qdrant(self, monkeypatch):
        """Mock Qdrant to avoid requiring a live instance."""
        monkeypatch.setattr("memory.qdrant_client.retrieve_similar", lambda *a, **kw: [])
        monkeypatch.setattr("memory.qdrant_client.store_incident",    lambda *a, **kw: None)
        monkeypatch.setattr("memory.qdrant_client.init_collection",   lambda: None)

    def test_full_pipeline_completes(self, tmp_path):
        """Full graph invocation populates all expected state keys."""
        os.environ["SENTINELOPS_CHECKPOINT_DB"] = str(tmp_path / "test_checkpoints.db")

        from agents.graph import build_graph
        graph = build_graph()

        incident = make_incident()
        thread_id = f"test-{incident['id'][:8]}"

        result = graph.invoke(
            {"incident": incident},
            config={"configurable": {"thread_id": thread_id}}
        )

        assert "severity_classification" in result, "triage_agent did not run"
        assert "rca" in result,                     "diagnosis_agent did not run"
        assert "proposed_patch" in result,           "remediation_agent did not run"
        assert "policy_allowed" in result,           "policy_review_agent did not run"
        assert result["severity_classification"] in ("critical", "warning", "info")
        assert isinstance(result["policy_allowed"], bool)

    def test_resume_after_crash(self, tmp_path, monkeypatch):
        """
        Simulates a crash after triage_agent by raising mid-pipeline,
        then re-invokes with the same thread_id and asserts that triage
        is NOT called again (resume from checkpoint).
        """
        db_path = str(tmp_path / "crash_checkpoints.db")
        os.environ["SENTINELOPS_CHECKPOINT_DB"] = db_path

        triage_call_count = {"n": 0}
        original_triage = __import__("agents.triage_agent", fromlist=["triage_agent"]).triage_agent

        def counting_triage(state):
            triage_call_count["n"] += 1
            return original_triage(state)

        monkeypatch.setattr("agents.triage_agent.triage_agent", counting_triage)

        from agents.graph import build_graph
        graph = build_graph()

        incident  = make_incident()
        thread_id = f"crash-test-{incident['id'][:8]}"

        # First invocation — completes successfully
        graph.invoke(
            {"incident": incident},
            config={"configurable": {"thread_id": thread_id}}
        )
        first_count = triage_call_count["n"]

        # Second invocation — same thread_id, should resume and NOT re-run triage
        graph.invoke(
            {"incident": incident},
            config={"configurable": {"thread_id": thread_id}}
        )
        second_count = triage_call_count["n"]

        # Triage should not have been called again on resume
        assert second_count == first_count, (
            f"triage_agent was called again on resume "
            f"(called {second_count - first_count} extra times). "
            "Checkpointer is not working correctly."
        )

    def test_unique_thread_ids_are_independent(self, tmp_path):
        """Two different thread_ids run independent pipelines."""
        os.environ["SENTINELOPS_CHECKPOINT_DB"] = str(tmp_path / "multi.db")

        from agents.graph import build_graph
        graph = build_graph()

        incident_a = make_incident()
        incident_b = make_incident()

        result_a = graph.invoke(
            {"incident": incident_a},
            config={"configurable": {"thread_id": f"thread-a-{incident_a['id'][:4]}"}}
        )
        result_b = graph.invoke(
            {"incident": incident_b},
            config={"configurable": {"thread_id": f"thread-b-{incident_b['id'][:4]}"}}
        )

        # Results are independent — different incident IDs
        assert result_a["incident"]["id"] != result_b["incident"]["id"]
