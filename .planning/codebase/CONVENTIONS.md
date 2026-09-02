# Coding Conventions

**Analysis Date:** 2026-07-24

## Naming Patterns

**Files:**
- Python modules: `snake_case.py` (e.g., `triage_agent.py`, `checkpointer_config.py`)
- Directories: `kebab-case` for packages with hyphens (e.g., `incident-normalizer/`, `gitops/`)
- Test files: `test_*.py` pattern in `tests/` directory (e.g., `test_incident_replay.py`, `test_selfheal.py`, `test_state_durability.py`)
- Config files: `*.yaml` / `*.yml` with kebab-case (e.g., `argocd-app.yaml`, `kind-config.yaml`)

**Functions:**
- snake_case for function names: `triage_agent`, `diagnosis_agent`, `remediation_agent`, `policy_review_agent`, `fetch_loki_logs`, `fetch_recent_metrics`, `dispatch_to_pipeline`, `query_opa`, `store_incident`, `retrieve_similar`, `init_collection`, `get_checkpointer`, `build_graph`, `open_remediation_pr`
- Private module-level: `_prefix` (e.g., `_qdrant_memory`, `_pipeline_pool`, `_graph_singleton`)

**Variables:**
- snake_case for local variables: `incident_summary`, `numeric_metrics`, `similar_context`, `triage_call_count`
- UPPER_SNAKE_CASE for module-level constants: `MODEL`, `SYSTEM_PROMPT`, `RISK_LEVELS`, `QDRANT_URL`, `COLLECTION`, `VECTOR_DIM`, `LOKI_URL`, `PIPELINE_WORKERS`
- Type hints used consistently: `dict[str, float]`, `list[str]`, `Optional[dict]`, `Tuple[bool, list[str]]`

**Types/Classes:**
- PascalCase for Pydantic models: `Incident`, `RCAResult`, `RemediationIntent`, `PolicyDecision`, `GitOpsChange`, `GraphState`
- TypedDict for LangGraph state: `GraphState(TypedDict, total=False)`

## Code Style

**Formatting:**
- Tool: **ruff** (enforced in CI via `ruff check .`)
- Line length: Not explicitly configured (ruff default 88)
- Import organization: Not explicitly configured (ruff default)

**Linting:**
- Tool: **ruff** (primary), **mypy** (type checking, run with `--ignore-missing-imports` on agents/memory)
- Type checking: mypy run on `agents/` and `memory/` only; ignores missing imports
- YAML linting: **yamllint** with custom config (`.yamllint.yaml`)

**Key yamllint rules (`.yamllint.yaml`):**
```yaml
extends: default
rules:
  line-length: { max: 120, level: warning }
  document-start: { present: false, level: warning }
  trailing-spaces: { level: error }
  empty-lines: { max: 2, level: warning }
  indentation: { spaces: 2, level: error }
  comments-indentation: { level: warning }
  key-duplicates: { level: error }
  braces: { level: warning }
  brackets: { level: warning }
  truthy: { level: warning }
  new-line-at-end-of-file: { level: error }
ignore: ".github/workflows/*.yaml"
```

**Import Organization:**
- Standard library imports first (`import json`, `import logging`, `import os`, `from typing import ...`)
- Third-party imports second (`import ollama`, `from fastapi import ...`, `from langgraph.graph import ...`, `from pydantic import ...`)
- Local imports last (`from agents.contracts import ...`, `from memory import qdrant_client`)
- Module-level docstrings at top of every file describing purpose, inputs/outputs, and usage

## Error Handling

**Patterns:**
1. **Try/except with specific exceptions** — `json.JSONDecodeError`, `requests.exceptions.ConnectionError`, `requests.exceptions.Timeout`, `GithubException`
2. **Fail-closed for security decisions** — OPA unreachable returns `False, ["OPA server unreachable — failing closed"]`
3. **Graceful degradation with logging** — Qdrant retrieval failure returns empty list, logs error
4. **Explicit validation after external calls** — LLM responses parsed with `json.loads()`, validated against allowed values, fallback to safe defaults
5. **Sentinel value handling** — `_fetch_errors` key popped from metrics dict before JSON serialization
6. **Context-aware logging** — Structured logging with `log.info("message", key=value)` pattern; includes incident IDs, workload names, classifications

**LLM Call Pattern (used in all agents):**
```python
try:
    response = ollama.chat(model=MODEL, messages=[...])
    raw = response["message"]["content"].strip()
    parsed = json.loads(raw)
    # Validate and use parsed fields
except json.JSONDecodeError:
    log.warning("LLM returned non-JSON — using fallback")
    # Fallback extraction logic
except Exception as exc:
    log.error("LLM call failed: %s", exc)
    # Safe default values
```

## Logging

**Framework:** Python `logging` module (standard library)

**Configuration:**
- Basic config in `incident-normalizer/webhook_server.py:36`: `logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")`
- Module-level loggers: `log = logging.getLogger("triage-agent")`, `log = logging.getLogger("remediation-agent")`, etc.

**Patterns:**
- Use `log.info("message", key=value)` for structured context
- Use `log.warning("message", detail)` for recoverable issues
- Use `log.error("message", exc)` for failures
- Include incident IDs, workload names, classifications in log context

## Comments

**When to Comment:**
- Module-level docstrings (mandatory): Purpose, responsibilities, input/output state keys, usage examples
- Complex logic sections with `# ── Section header ─────────────────────────────────────────`
- Non-obvious workarounds with explanation (e.g., checkpointer_config.py BUG FIX NOTE)
- Type ignores with explanation: `# type: ignore[return-value]`

**JSDoc/TSDoc:** Not used (Python codebase). Pydantic `Field(description="...")` serves as inline schema documentation.

## Function Design

**Size:** Functions are small and single-purpose (typically 20-80 lines). LangGraph node functions follow pattern: read state → process → write state → return state.

**Parameters:**
- Single `state: dict` parameter for LangGraph nodes
- Explicit typed parameters for utility functions
- `*args, **kw` used for monkeypatching compatibility in `retrieve_similar`, `store_incident`

**Return Values:**
- LangGraph nodes: always return modified `state` dict
- Utility functions: typed returns (`list[dict]`, `tuple[bool, list[str]]`, `str`)
- Early returns used for error/deny paths

## Module Design

**Exports:**
- Minimal `__init__.py` files (often empty or single comment)
- Public API defined by what's imported in `__init__.py` or used by other modules
- Module-level functions preferred over classes (functional style)

**Barrel Files:** Not used. Direct imports from modules: `from agents.triage_agent import triage_agent`

**Lazy Singletons:**
- Module-level `_client`, `_embedder`, `_graph_singleton` with `get_*()` accessors
- Used for Qdrant client, FastEmbed embedder, LangGraph compiled graph
- Ensures env vars can be set before initialization (critical for tests)

**Monkeypatch-Friendly Imports:**
- Import modules, not functions: `from memory import qdrant_client as _qdrant_memory`
- Resolve at call-time: `retrieve_similar = _qdrant_memory.retrieve_similar`
- Enables pytest `monkeypatch.setattr("memory.qdrant_client.retrieve_similar", mock_fn)`

## Configuration

**Environment Variables:**
- All external service URLs configurable via env: `OLLAMA_MODEL`, `QDRANT_URL`, `LOKI_URL`, `PROMETHEUS_URL`, `OPA_URL`, `GITHUB_TOKEN`, `GITHUB_REPO`, `SLACK_WEBHOOK_URL`
- Defaults in code, override via env
- Required vars validated at call time with clear error messages

**Settings Objects:** Not used. Direct `os.getenv()` calls with defaults.

## Anti-Patterns

### Shell Command Injection in LLM Output

**What happens:** LLM proposes a YAML patch containing `kubectl`, `bash`, `sh -c`, etc.
**Why it's wrong:** Violates GitOps principle — all changes must be declarative manifests, not imperative commands.
**Do this instead:** Validation in `remediation_agent.py:112-117` rejects patches containing shell indicators, marks as `rejected` with `risk_level=high`.

### Import-Time Side Effects

**What happens:** Creating DB connections, opening files, or starting threads at module import.
**Why it's wrong:** Breaks test isolation; prevents env var configuration before initialization.
**Do this instead:** Lazy singletons via `get_*()` functions (see `checkpointer_config.py`, `qdrant_client.py`, `graph.py`).

### Direct Module Function Import for Monkeypatching

**What happens:** `from memory.qdrant_client import retrieve_similar` at module level.
**Why it's wrong:** Binds function at import time; pytest `monkeypatch` cannot affect.
**Do this instead:** Import module, resolve at call-time (see `diagnosis_agent.py:28`, `policy_review_agent.py:185-186`).

---

*Convention analysis: 2026-07-24*