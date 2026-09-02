# Testing Patterns

**Analysis Date:** 2026-07-24

## Test Framework

**Runner:** `pytest` (v9.1.1)

**Config:** No `pytest.ini` or `pyproject.toml` pytest section. Configuration via:
- `tests/conftest.py` — shared fixtures, import path setup, warning filters
- CLI flags in CI: `pytest tests/ -v --cov=agents --cov=incident_normalizer --cov=memory --cov-fail-under=50`

**Assertion Library:** Built-in `assert` statements (pytest rewrites)

**Run Commands:**
```bash
pytest tests/ -v                          # All tests
pytest tests/test_incident_replay.py -v   # Unit tests only
pytest tests/test_state_durability.py -v  # State durability tests
pytest tests/test_selfheal.py -v -s --timeout=300  # Integration tests (requires k8s)
pytest --cov=agents --cov=incident_normalizer --cov=memory --cov-fail-under=50
```

**Coverage:** Minimum 50% enforced in CI (`--cov-fail-under=50`). Reports: `term-missing`, `xml` (for Codecov).

## Test File Organization

**Location:** `tests/` directory (flat structure)

**Naming:** `test_*.py` prefix

**Structure:**
```
tests/
├── conftest.py              # Shared fixtures, path setup, warning filters
├── test_incident_replay.py  # Unit tests: webhook parsing, Incident schema
├── test_state_durability.py # Unit tests: LangGraph checkpoint resume
└── test_selfheal.py         # Integration tests: Argo CD selfHeal (requires kind cluster)
```

**Module-Level pytestmark:** Used in `test_selfheal.py` to skip entire module if no k8s cluster:
```python
pytestmark = pytest.mark.skipif(not _k8s_available(), reason="No Kubernetes cluster reachable")
```

## Test Structure

### Suite Organization
- **Classes** group related tests: `TestWebhookParsing`, `TestIncidentSchema`, `TestStateDurability`, `TestArgoCDSelfHeal`
- **Methods** named `test_<behavior>`: `test_firing_alert_returns_processed`, `test_resume_after_crash`, `test_selfheal_reverts_manual_scale`
- **Docstrings** on classes and methods explain intent

### Fixtures (conftest.py)
```python
# Path setup: adds repo root to sys.path, registers incident-normalizer as importable "incident_normalizer"
# Warning filters: suppresses Pydantic v2 .dict() deprecation, starlette deprecation
```

### Fixtures (per-test-file)
- **Autouse fixtures** in test classes for external dependency mocking:
```python
@pytest.fixture(autouse=True)
def mock_llm(self, monkeypatch):
    call_counter = {"n": 0}
    def mock_chat(model, messages):
        call_counter["n"] += 1
        # Determine agent from system prompt content
        if "triage" in messages[0]["content"].lower():
            return {"message": {"content": MOCK_TRIAGE_RESPONSE}}
    monkeypatch.setattr("ollama.chat", mock_chat)
    self.call_counter = call_counter
```

- **Shared fixtures** for k8s client (`k8s_client` module-scoped)

## Mocking

**Framework:** `pytest-mock` / built-in `monkeypatch` fixture

**Patterns:**

### 1. Monkeypatch External Services (Ollama, OPA, GitHub, Qdrant)
```python
monkeypatch.setattr("ollama.chat", mock_chat)
monkeypatch.setattr("requests.post", mock_opa_post)
monkeypatch.setattr("agents.gitops_bridge.open_remediation_pr", lambda **kw: "https://github.com/mock/pull/1")
monkeypatch.setattr("memory.qdrant_client.retrieve_similar", lambda *a, **kw: [])
monkeypatch.setattr("memory.qdrant_client.store_incident", lambda *a, **kw: None)
monkeypatch.setattr("memory.qdrant_client.init_collection", lambda: None)
```

### 2. Target Module-Level Import Aliases (Critical)
The codebase imports modules (not functions) to enable monkeypatching:
```python
# In diagnosis_agent.py, policy_review_agent.py:
from memory import qdrant_client as _qdrant_memory
# At call time:
retrieve_similar = _qdrant_memory.retrieve_similar
similar = retrieve_similar(query_text, k=3)
```
Tests patch the module attribute:
```python
monkeypatch.setattr("memory.qdrant_client.retrieve_similar", mock_fn)
```

### 3. Deterministic LLM Responses
Mock `ollama.chat` returns pre-defined JSON strings per agent (matched by system prompt keywords):
```python
MOCK_TRIAGE_RESPONSE = json.dumps({"severity": "critical", "reasoning": "Pod is crash looping due to OOMKill."})
MOCK_DIAGNOSIS_RESPONSE = json.dumps({"probable_cause": "...", "confidence": 0.85, ...})
MOCK_REMEDIATION_RESPONSE = json.dumps({"patch_type": "patch_memory_limit", "risk_level": "medium", ...})
```

### 4. Environment Variable Isolation
```python
os.environ["SENTINELOPS_CHECKPOINT_DB"] = str(tmp_path / "test_checkpoints.db")
```

## Test Types

### Unit Tests (`test_incident_replay.py`, `test_state_durability.py`)
- **Scope:** Single module / function behavior
- **Dependencies:** Fully mocked (Loki, Prometheus, Ollama, OPA, GitHub, Qdrant, LangGraph checkpointer DB)
- **Speed:** Fast (< 1s each)
- **Run in CI:** Yes (every PR)

### Integration Tests (`test_selfheal.py`)
- **Scope:** End-to-end Argo CD selfHeal behavior on real kind cluster
- **Dependencies:** Live Kubernetes cluster (kind), Argo CD, deployed manifests
- **Speed:** Slow (300s timeout)
- **Run in CI:** Only on `main` branch push (`if: github.event_name == 'push' && github.ref == 'refs/heads/main'`)
- **Skipped locally** unless `KUBECONFIG` points to sentinelops kind cluster

### Policy Tests (OPA Rego)
- **Location:** `policies/remediation_test.rego`
- **Run:** `opa test policies/remediation.rego policies/remediation_test.rego -v`
- **Run in CI:** Yes (in lint-and-test job)

## Common Patterns

### Async Testing
Not used — webhook tests use `TestClient` (sync FastAPI test client).

### Test Data / Fixtures
- **Inline dicts** for AlertManager payloads (`SAMPLE_ALERTMANAGER_PAYLOAD`, `RESOLVED_PAYLOAD`)
- **Factory function** for incidents: `make_incident(incident_id)` in `test_state_durability.py`
- **Constants** for expected values: `GIT_REPLICA_COUNT = 1`, `DRIFT_REPLICA_COUNT = 5`

### Assertions
- Standard `assert` with descriptive messages:
```python
assert body["count"] == 1, "Expected 1 incident processed"
assert reverted, f"Argo CD selfHeal did NOT revert within {SELFHEAL_TIMEOUT_S}s"
```
- `pytest.approx` not used (no floating-point comparisons in tests)

### Parametrization
Not currently used. Tests are written as separate methods.

### Time/Flaky Test Handling
- `@pytest.mark.timeout(SELFHEAL_TIMEOUT_S)` on integration tests
- Polling loops with explicit sleeps and elapsed tracking (not retries)
- Print statements for progress visibility in integration tests

## Coverage

**Target:** ≥ 50% (enforced in CI)

**Measured packages:** `agents`, `incident_normalizer`, `memory`

**Excluded:** `tests/`, `sample-app/`, `infra/`, `observability/`, `policies/`

**View Coverage:**
```bash
pytest --cov=agents --cov=incident_normalizer --cov=memory --cov-report=term-missing
```

**Typical Gaps:**
- Error branches in external service calls (OPA unreachable, GitHub API errors)
- Slack notification path (requires `SLACK_WEBHOOK_URL`)
- Auto-merge logic (requires `AUTO_MERGE_LOW_RISK=true`)
- Qdrant collection recreation path

## Anti-Patterns Avoided

### No Real External Calls in Unit Tests
All external dependencies (Ollama, Loki, Prometheus, OPA, GitHub, Qdrant, k8s API) are mocked via `monkeypatch` in `autouse` fixtures.

### No Shared Mutable State Between Tests
- Each test gets fresh `tmp_path` for SQLite checkpoint DB
- `monkeypatch` reverts automatically after each test
- Module-level singletons (`_graph_singleton`, `_client`, `_embedder`) are reinitialized per test via env var changes

### No Test Order Dependencies
- Tests use unique `thread_id` / incident IDs
- Integration test validates preconditions before acting

---

*Testing analysis: 2026-07-24*