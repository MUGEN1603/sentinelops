<!-- refreshed: 2026-07-24 -->
# Architecture

**Analysis Date:** 2026-07-24

## System Overview

```text
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                        KUBERNETES CLUSTER (kind, 3-node)                             │
│                                                                                      │
│  ┌─────────────────┐  ┌──────────────────────┐  ┌────────────────────────────────┐  │
│  │  Sample App     │  │  Observability Stack │  │  Network Policies              │  │
│  │  (FastAPI)      │  │  (Prometheus Stack)  │  │  (default-deny + explicit     │  │
│  │  `sample-app/`  │  │  + Loki + OTel       │  │   allow rules)                 │  │
│  │                 │  │  `observability/`    │  │  `infra/network-policies.yaml` │  │
│  └────────┬────────┘  └──────────┬───────────┘  └────────────────────────────────┘  │
│           │                      │                                              │
│           │ logs,metrics         │ alert webhook, metrics scrape                  │
│           ▼                      ▼                                              │
└───────────┼──────────────────────┼──────────────────────────────────────────────────┘
            │                      │
            │              ┌───────▼────────┐
            │              │  AlertManager  │
            │              │  (webhook →)   │
            │              └───────┬────────┘
            │                      │ POST /webhook
            ▼                      ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                    INCIDENT NORMALIZER (FastAPI :8000)                                │
│  `incident-normalizer/webhook_server.py`                                              │
│  • Receives AlertManager webhook                                                      │
│  • Enriches with Loki logs + Prometheus metrics                                      │
│  • Builds typed Incident{} Pydantic object                                           │
│  • Dispatches to LangGraph pipeline (bounded thread pool)                            │
└────────────────────────────────────┬──────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                    LANGGRAPH MULTI-AGENT PIPELINE                                     │
│  `agents/graph.py` — SQLite checkpointer (durable, resumable state)                  │
│                                                                                      │
│  ┌────────────┐    ┌──────────────┐    ┌────────────────┐    ┌───────────────────┐  │
│  │ triage_agent│───▶│diagnosis_agent│───▶│remediation_agent│───▶│policy_review_agent│  │
│  │            │    │              │    │               │    │                   │  │
│  │ Classify:  │    │ RCA + Qdrant │    │ YAML patch    │    │ OPA eval + GitHub │  │
│  │ critical/  │    │ RAG retrieval│    │ only (no shell)│   │ PR creation       │  │
│  │ warning/   │    │              │    │ risk_level    │    │                   │  │
│  │ info       │    │              │    │               │    │                   │  │
│  └────────────┘    └──────────────┘    └────────────────┘    └────────┬────────┘  │
│                                                                        │            │
│                                                              ┌─────────┴────────┐  │
│                                                              ▼                ▼  │
┌─────────────────────────────────────────────────────────────────────────────────┐
│  allowed=true                                    allowed=false                    │
│       │                                                      │                   │
│       ▼                                                      ▼                   │
│ ┌───────────────┐                                ┌─────────────────┐             │
│ │ GitHub PR     │                                │ Log rejection   │             │
│ │ auto-fix/<id> │                                │ Slack notify    │             │
│ │ + YAML diff   │                                │ (optional)      │             │
│ └───────┬───────┘                                └─────────────────┘             │
│         │                                                                         │
└─────────┼─────────────────────────────────────────────────────────────────────────┘
          ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                         ARGO CD (GitOps Reconciler)                                   │
│  `gitops/argocd-app.yaml` — selfHeal: true, prune: true                             │
│  Watches `gitops/manifests/` on main branch                                          │
│  • Merges PR → syncs cluster                                                         │
│  • Manual drift (`kubectl scale`) → auto-revert within ~3 min                        │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| **kind Cluster** | 3-node local K8s (1 CP, 2 workers), images pre-loaded | `infra/kind-config.yaml` |
| **Prometheus + AlertManager** | Metrics collection, alert evaluation, webhook dispatch | `observability/prometheus-values.yaml`, `observability/alert-rules.yaml` |
| **Loki + Promtail** | Log aggregation, LogQL querying | `observability/loki-values.yaml` |
| **OTel Collector (DaemonSet)** | Metrics/logs/traces collection, k8s attribute enrichment | `observability/otel-collector-values.yaml` |
| **Sample App** | Fault-injection FastAPI app (`/crash`, `/leak-memory`, `/slow`) | `sample-app/app.py` |
| **Incident Normalizer** | Webhook receiver → enrich with logs/metrics → dispatch pipeline | `incident-normalizer/webhook_server.py` |
| **LangGraph Pipeline** | 4-agent orchestration with SQLite checkpoint durability | `agents/graph.py` |
| **triage_agent** | Classify incident severity (critical/warning/info) via LLM | `agents/triage_agent.py` |
| **diagnosis_agent** | RCA with Qdrant RAG (retrieve similar past incidents) | `agents/diagnosis_agent.py` |
| **remediation_agent** | Propose YAML strategic-merge patch (never shell commands) | `agents/remediation_agent.py` |
| **policy_review_agent** | OPA policy gate → GitHub PR creation + Qdrant storage | `agents/policy_review_agent.py` |
| **Qdrant Vector Memory** | Store/resolve incidents using 384-dim embeddings (FastEmbed) | `memory/qdrant_client.py` |
| **OPA Policy Engine** | Rego policy: allow known actions, deny high-risk/protected-ns | `policies/remediation.rego` |
| **GitOps Bridge** | GitHub PR creation on `auto-fix/<incident-id>` branch | `agents/gitops_bridge.py` |
| **Argo CD** | GitOps reconciler: selfHeal + prune on `gitops/manifests/` | `gitops/argocd-app.yaml` |

## Pattern Overview

**Overall:** Event-driven, agent-based AIOps pipeline with GitOps-only cluster mutation

**Key Characteristics:**
- **Durable execution**: SQLite checkpointer enables crash recovery; same `thread_id` resumes from last completed node
- **GitOps-only mutations**: No direct `kubectl` — all changes via GitHub PR → Argo CD sync
- **Policy-gated approval**: OPA Rego enforces allow-list, risk-level, and protected-namespace rules; fail-closed on OPA unavailability
- **RAG-backed diagnosis**: Qdrant vector search (cosine similarity, 384-dim) provides historical context to diagnosis LLM
- **Fail-safe LLM interactions**: JSON parsing with fallback extraction; shell command injection rejected explicitly
- **Bounded concurrency**: ThreadPoolExecutor (default 2 workers) prevents unbounded thread spawn under alert bursts

## Layers

### 1. Infrastructure Layer
- **Purpose**: Provision and configure the Kubernetes cluster and platform services
- **Location**: `infra/`
- **Contains**: `kind-config.yaml` (cluster topology), `namespaces.yaml`, `rbac.yaml`, `network-policies.yaml`, `secrets.yaml`
- **Depends on**: None (foundation)
- **Used by**: All upper layers

### 2. Observability Layer
- **Purpose**: Collect, store, and alert on metrics, logs, and traces
- **Location**: `observability/`
- **Contains**: Helm values for Prometheus stack, Loki, OTel Collector; PrometheusRule CRDs for alerts
- **Depends on**: Infrastructure layer (cluster, namespaces)
- **Used by**: Incident Normalizer (queries Loki/Prometheus), AlertManager (webhook source)

### 3. Application Layer
- **Purpose**: Workload that generates incidents for the platform to detect/remediate
- **Location**: `sample-app/`
- **Contains**: FastAPI app with fault injection endpoints, Dockerfile, K8s manifest
- **Depends on**: Infrastructure (cluster), Observability (scraped by Prometheus, logged to Loki)
- **Used by**: Incident Normalizer (target of alerts)

### 4. Incident Processing Layer
- **Purpose**: Receive alerts, enrich with context, build canonical Incident object
- **Location**: `incident-normalizer/`
- **Contains**: FastAPI webhook server, Loki/Prometheus query logic, pipeline dispatcher
- **Depends on**: Observability (Loki, Prometheus endpoints), Agents (LangGraph pipeline)
- **Used by**: AlertManager (webhook target)

### 5. Agent Orchestration Layer
- **Purpose**: Multi-agent LLM pipeline for triage, diagnosis, remediation, policy review
- **Location**: `agents/`
- **Contains**: 
  - `graph.py` — LangGraph StateGraph compilation with SQLite checkpointer
  - `contracts.py` — Pydantic/TypedDict schemas (Incident, GraphState, GitOpsChange, etc.)
  - `checkpointer_config.py` — SqliteSaver with `check_same_thread=False`
  - Four agent node modules (`triage_agent.py`, `diagnosis_agent.py`, `remediation_agent.py`, `policy_review_agent.py`)
  - `gitops_bridge.py` — GitHub PR creation
- **Depends on**: Contracts, Memory (Qdrant), Policy (OPA), GitHub API, LLM (Ollama)
- **Used by**: Incident Normalizer (dispatches incidents)

### 6. Memory Layer
- **Purpose**: Vector similarity search for RAG-based root cause analysis
- **Location**: `memory/`
- **Contains**: `qdrant_client.py` — Qdrant client, FastEmbed embedder, collection management
- **Depends on**: Qdrant service (Docker), FastEmbed model cache
- **Used by**: diagnosis_agent (retrieve), policy_review_agent (store)

### 7. Policy Layer
- **Purpose**: Declarative guardrails on remediation actions
- **Location**: `policies/`
- **Contains**: `remediation.rego` (OPA policy), `remediation_test.rego` (OPA unit tests)
- **Depends on**: OPA server (Docker)
- **Used by**: policy_review_agent (HTTP POST to `/v1/data/sentinelops/remediation/allow`)

### 8. GitOps Layer
- **Purpose**: Declarative cluster state reconciliation
- **Location**: `gitops/`
- **Contains**: 
  - `argocd-app.yaml` — Argo CD Application (selfHeal=true, prune=true)
  - `manifests/sample-app-deployment.yaml` — Canonical Deployment manifest
- **Depends on**: Argo CD controller, GitHub repo
- **Used by**: policy_review_agent (writes PR to `gitops/manifests/`), Argo CD (watches `gitops/manifests/`)

## Data Flow

### Primary Request Path: Alert → Resolution

```
T+0m    /leak-memory ×3 → OOMKill → Pod restarts
T+0m    Promtail captures OOMKill logs → Loki
T+0m    Kubelet metrics spike → Prometheus scrapes memory_usage > 90%
T+3m    HighMemoryUsage alert fires → AlertManager POST to Incident Normalizer
T+3m    Incident Normalizer:
         1. fetch_recent_logs(pod, namespace) → Loki LogQL query
         2. fetch_recent_metrics(pod, namespace) → Prometheus instant queries
         3. Build Incident{} Pydantic model
         4. ThreadPoolExecutor.submit(dispatch_to_pipeline, incident)
T+3m    LangGraph Pipeline (thread_id = incident.id):
         1. triage_agent:    LLM classifies severity → writes severity_classification, triage_summary
         2. diagnosis_agent: Query Qdrant (top-3 similar) → LLM RCA → writes rca, similar_incidents, rca_confidence
         3. remediation_agent: LLM proposes YAML patch + risk_level + rollback_hint → writes proposed_patch, patch_type, risk_level
         4. policy_review_agent:
              a. POST to OPA {action, risk_level, namespace} → OPA
              b. If allowed: open GitHub PR via gitops_bridge → writes gitops_change
              c. If denied: log + optional Slack → writes policy_allowed=false
              d. Store incident in Qdrant for future RAG
T+Xm    Human reviews PR → merges (or auto-merge if risk_level=low & AUTO_MERGE_LOW_RISK=true)
T+Xm    Argo CD detects main branch change → syncs cluster
T+X+3m  Cluster reconciled with new manifest → incident resolved
T+X+3m  MTTR recorded in runbook
```

### Secondary Flow: Self-Healing Drift Detection

```
Manual drift: kubectl scale deployment sample-app -n apps --replicas=5
         │
         ▼
Argo CD Application (selfHeal=true, prune=true) detects divergence
         │
         ▼
Reconciles cluster to match `gitops/manifests/sample-app-deployment.yaml` (replicas=1)
         │
         ▼
Drift reverted within sync interval (~3 min default)
```

### State Management

- **SQLite Checkpointer** (`agents/checkpointer_config.py`): 
  - `SqliteSaver(sqlite3.connect(..., check_same_thread=False))`
  - Persists every node completion to `sentinelops_checkpoints.db`
  - Re-invoking with same `thread_id` resumes from last completed node
  - Production upgrade path: swap to `PostgresSaver` (API identical)

- **GraphState** (`agents/contracts.py`): TypedDict with `total=False` — each node writes only its keys; LangGraph merges partial returns

## Key Abstractions

### Incident (Canonical Input)
- **Purpose**: Single source of truth for an alert + enriched context
- **Examples**: `agents/contracts.py:13-31` — Pydantic model with fields: id, started_at, namespace, workload, severity, alert_labels, recent_logs, recent_metrics, metric_fetch_errors, k8s_objects, trace_refs
- **Pattern**: Built by Incident Normalizer; passed as `{"incident": incident.model_dump()}` to graph.invoke()

### GraphState (Pipeline State)
- **Purpose**: Mutable dict passed between LangGraph nodes; persists across crashes via checkpointer
- **Examples**: `agents/contracts.py:92-123` — TypedDict with keys written by each agent node
- **Pattern**: Each agent reads subset, writes its outputs; `total=False` allows optional keys

### GitOpsChange (Audit Record)
- **Purpose**: Track PR creation and merge status for runbook/MTTR calculation
- **Examples**: `agents/contracts.py:69-82` — branch, files_changed, pr_url, merge_status, created_at
- **Pattern**: Written by policy_review_agent on allow; stored in graph state + runbook

### Remediation Patch (YAML Strategic-Merge)
- **Purpose**: Declarative cluster change — never imperative commands
- **Examples**: `agents/remediation_agent.py:46-52` — LLM returns `patch_yaml` string
- **Pattern**: LLM constrained by SYSTEM_PROMPT to output only YAML patch; shell commands explicitly rejected

## Entry Points

### Incident Normalizer Webhook
- **Location**: `incident-normalizer/webhook_server.py:228` — `@app.post("/webhook")`
- **Triggers**: AlertManager POST (firing alerts only; resolved alerts skipped)
- **Responsibilities**: Parse payload, enrich from Loki/Prometheus, build Incident, dispatch to pipeline via ThreadPoolExecutor

### LangGraph Pipeline
- **Location**: `agents/graph.py:110` — `get_graph()` returns compiled StateGraph singleton
- **Triggers**: `graph.invoke({"incident": incident_dict}, config={"configurable": {"thread_id": incident_id}})`
- **Responsibilities**: Execute 4-node pipeline with checkpoint persistence

### GitHub PR Bridge
- **Location**: `agents/gitops_bridge.py:54` — `open_remediation_pr()`
- **Triggers**: Called by policy_review_agent when OPA allows
- **Responsibilities**: Create branch `auto-fix/<id>`, commit patched manifest, open PR with audit body, optional auto-merge

### Argo CD Application
- **Location**: `gitops/argocd-app.yaml` — applied via `kubectl apply -f`
- **Triggers**: Git push to `main` branch (polling webhook) or manual `argocd app sync`
- **Responsibilities**: Reconcile `gitops/manifests/` to `apps` namespace; selfHeal reverts drift

## Architectural Constraints

- **Threading**: Incident Normalizer uses `ThreadPoolExecutor(max_workers=2)` for pipeline dispatch; SQLite checkpointer requires `check_same_thread=False` because invocations run in worker threads
- **Global State**: 
  - `_graph_singleton` in `agents/graph.py:107` — lazy-loaded compiled graph
  - `_client` and `_embedder` in `memory/qdrant_client.py:41-42` — cached Qdrant/FastEmbed clients
  - These are process-lifetime singletons; safe for single-process deployment
- **Circular Imports**: Avoided by importing modules (not functions) at module scope in agents:
  - `diagnosis_agent.py:28` — `from memory import qdrant_client as _qdrant_memory` then resolves `retrieve_similar = _qdrant_memory.retrieve_similar` at call time (allows pytest monkeypatch)
  - `policy_review_agent.py:185` — same pattern for `store_incident`
  - `gitops_bridge` imported inside `policy_review_agent.py:146` function body to avoid cycle
- **Fail-Closed Policy**: OPA unreachable → `policy_review_agent` defaults to `allow=false` with reason "OPA server unreachable — failing closed" (`policy_review_agent.py:86-89`)
- **Shell Injection Guard**: `remediation_agent.py:112-117` rejects any patch containing `kubectl`, `bash`, `sh -c`, `exec`, `os.system`, `subprocess`

## Anti-Patterns

### ❌ Direct kubectl/Shell Mutations in Remediation
**What happens**: LLM proposes a shell command or `kubectl` invocation inside the patch YAML
**Why it's wrong**: Bypasses GitOps audit trail, no policy review, no rollback via Git revert
**Do this instead**: `remediation_agent` SYSTEM_PROMPT mandates YAML strategic-merge patch only; shell indicators rejected at `remediation_agent.py:112-117`

### ❌ Mutable GraphState Keys Across Nodes
**What happens**: Multiple nodes write the same state key causing merge conflicts or silent overwrites
**Why it's wrong**: LangGraph merges partial returns; last write wins, losing earlier data
**Do this instead**: Each agent writes distinct keys per `GraphState` contract (`contracts.py:96-109`); no key overlap

### ❌ Changing Embedding Model Without Recreating Qdrant Collection
**What happens**: `fastembed` model changed (e.g., to 768-dim) but collection remains 384-dim → `store_incident()` fails with dimension mismatch
**Why it's wrong**: Vector dimension is fixed at collection creation
**Do this instead**: Call `recreate_collection()` after model change (`memory/qdrant_client.py:85-94`); document in `qdrant_client.py:7-10`

### ❌ Ignoring Metric Fetch Failures Silently
**What happens**: Prometheus query fails but incident proceeds with empty metrics → RCA has blind spots
**Why it's wrong**: Downstream agents assume metrics exist, produce overconfident RCA
**Do this instead**: `webhook_server.py:183-191` surfaces failed metrics in `metric_fetch_errors` list; `diagnosis_agent.py:99-119` sanitizes and injects context note into LLM prompt

## Error Handling

**Strategy**: Graceful degradation with explicit failure signals

**Patterns:**
- **LLM call failures**: Each agent wraps `ollama.chat()` in try/except; on error, writes fallback state (e.g., `classification=incident.severity`, `confidence=0.0`, `patch="# ERROR: <exc>"`) and logs error — pipeline continues
- **JSON parsing failures**: Try `json.loads()`; on `JSONDecodeError`, attempt regex extraction of YAML block; if all fails, use raw text with low confidence (`diagnosis_agent.py:158-161`, `remediation_agent.py:122-129`)
- **External service unreachable**: 
  - OPA: fail-closed (`policy_review_agent.py:86-89`)
  - Loki/Prometheus: return empty results + error sentinel in `metric_fetch_errors` (`webhook_server.py:99-104`, `webhook_server.py:167-181`)
  - Qdrant: log warning, return empty list (non-blocking for retrieval); log warning on store (non-blocking) (`memory/qdrant_client.py:189-191`, `policy_review_agent.py:205-206`)
  - GitHub: capture exception, write error status to `gitops_change.merge_status` (`gitops_bridge.py:116-117`, `policy_review_agent.py:172-179`)
- **Checkpoint corruption**: New `thread_id` starts fresh; old checkpoints isolated by thread_id

## Cross-Cutting Concerns

**Logging**: Python `logging` module with module-level loggers (`logging.getLogger("module-name")`); structured via `log.info("msg", key=val)`; FastAPI access logs via uvicorn

**Validation**: Pydantic models for Incident, RCAResult, RemediationIntent, PolicyDecision, GitOpsChange (`agents/contracts.py`); `Incident` validated on webhook receipt

**Authentication**: 
- GitHub: `GITHUB_TOKEN` env var (PAT with repo:write) — never hardcoded
- OPA: No auth in dev (localhost); production should use mTLS
- Qdrant/Ollama: No auth in local dev; production adds auth headers

---

*Architecture analysis: 2026-07-24*