"""
agents/graph.py — SentinelOps LangGraph agent pipeline

Pipeline: triage → diagnosis → remediation → policy_review → END

Key design properties:
  ✓ Durable state via SQLite checkpointer (resumes after crash on same thread_id)
  ✓ All agents read/write a single shared GraphState dict
  ✓ No direct cluster mutations — all changes go through GitOps bridge → PR → Argo CD

Exit check:
    python -c "
    from agents.graph import graph
    import uuid

    sample_incident = {
        'id': str(uuid.uuid4()),
        'started_at': '2024-01-01T00:00:00Z',
        'namespace': 'apps',
        'workload': 'sample-app-abc123',
        'kind': 'Pod',
        'severity': 'critical',
        'alert_labels': {'alertname': 'PodCrashLooping', 'namespace': 'apps'},
        'recent_logs': ['ERROR: out of memory', 'OOMKilled'],
        'recent_metrics': {'memory_usage_bytes': 134217728},
        'k8s_objects': [],
        'trace_refs': []
    }

    thread_id = 'test-' + sample_incident['id'][:8]
    result = graph.invoke(
        {'incident': sample_incident},
        config={'configurable': {'thread_id': thread_id}}
    )
    assert 'rca' in result,              'diagnosis_agent did not write rca'
    assert 'proposed_patch' in result,   'remediation_agent did not write proposed_patch'
    assert 'policy_allowed' in result,   'policy_review_agent did not write policy_allowed'
    print('✓ Pipeline complete')
    print('  severity_classification:', result.get('severity_classification'))
    print('  policy_allowed:',         result.get('policy_allowed'))
    print('  pr_url:',                 result.get('gitops_change', {}).get('pr_url'))
    "
"""

import logging

from langgraph.graph import StateGraph, END

from agents.contracts import GraphState
from agents.checkpointer_config import get_checkpointer
from agents.triage_agent import triage_agent
from agents.diagnosis_agent import diagnosis_agent
from agents.remediation_agent import remediation_agent
from agents.policy_review_agent import policy_review_agent

log = logging.getLogger("agent-graph")


def build_graph() -> StateGraph:
    """
    Construct and compile the SentinelOps agent graph.

    Node order:
        triage_agent
            → diagnosis_agent    (enriched with Qdrant RAG context)
            → remediation_agent  (proposes YAML patch)
            → policy_review_agent (OPA gate + GitHub PR)
            → END

    The graph is compiled with a SQLite checkpointer so that partial progress
    is persisted to disk. If the process crashes between any two nodes, re-invoking
    with the same thread_id resumes from the last completed node.

    NOTE: A NEW graph/checkpointer is built on every call. This is intentional so
    that tests (and the webhook server) pick up a freshly-set
    `SENTINELOPS_CHECKPOINT_DB` env var. Use `get_graph()` for a cached singleton
    within a single process.
    """
    workflow = StateGraph(GraphState)

    # ── Register nodes ────────────────────────────────────────────────────────
    workflow.add_node("triage",        triage_agent)
    workflow.add_node("diagnosis",     diagnosis_agent)
    workflow.add_node("remediation",   remediation_agent)
    workflow.add_node("policy_review", policy_review_agent)

    # ── Define edges (linear pipeline) ───────────────────────────────────────
    workflow.set_entry_point("triage")
    workflow.add_edge("triage",        "diagnosis")
    workflow.add_edge("diagnosis",     "remediation")
    workflow.add_edge("remediation",   "policy_review")
    workflow.add_edge("policy_review", END)

    # ── Compile with checkpointer ─────────────────────────────────────────────
    checkpointer = get_checkpointer()
    compiled = workflow.compile(checkpointer=checkpointer)

    log.info("SentinelOps agent graph compiled (4 nodes, SQLite checkpointer)")
    return compiled


# ── Lazily-built process singleton ────────────────────────────────────────────
# We avoid `graph = build_graph()` at module load time because that would open the
# SQLite checkpoint DB before a test has a chance to set SENTINELOPS_CHECKPOINT_DB.
# `get_graph()` builds the singleton on first call (after env is configured).
_graph_singleton = None


def get_graph():
    """Return the process-wide compiled graph singleton, built lazily on first call."""
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_graph()
    return _graph_singleton


# Backwards-compat: `from agents.graph import graph` still works in the webhook
# server and runbook samples. Use `get_graph()` in tests to control DB path.
def __getattr__(name):
    if name == "graph":
        return get_graph()
    raise AttributeError(f"module 'agents.graph' has no attribute {name!r}")
