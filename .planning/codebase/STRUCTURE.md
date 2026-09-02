# Codebase Structure

**Analysis Date:** 2026-07-24

## Directory Layout

```
sentinelops/
├── agents/                    # LangGraph multi-agent pipeline
│   ├── __init__.py
│   ├── checkpointer_config.py # SQLite checkpointer for durable state
│   ├── contracts.py           # Pydantic/TypedDict schemas (Incident, GraphState, etc.)
│   ├── diagnosis_agent.py     # RCA with Qdrant RAG retrieval
│   ├── gitops_bridge.py       # GitHub PR creation for approved remediations
│   ├── graph.py               # LangGraph StateGraph compilation (4 nodes)
│   ├── policy_review_agent.py # OPA policy gate + PR dispatch + Qdrant store
│   ├── remediation_agent.py   # YAML patch proposal (no shell commands)
│   └── triage_agent.py        # Severity classification via LLM
├── docs/
│   ├── architecture.md        # Architecture reference (source of truth for this doc)
│   └── runbook.md             # 12-step validation checklist + troubleshooting
├── gitops/                    # Argo CD GitOps reconciliation
│   ├── argocd-app.yaml        # Argo CD Application (selfHeal=true, prune=true)
│   └── manifests/
│       └── sample-app-deployment.yaml  # Canonical Deployment manifest
├── incident-normalizer/       # AlertManager webhook receiver + enrichment
│   ├── __init__.py
│   └── webhook_server.py      # FastAPI /webhook → Loki/Prometheus → pipeline
├── infra/                     # Cluster provisioning & platform config
│   ├── kind-config.yaml       # 3-node kind cluster (1 CP, 2 workers)
│   ├── namespaces.yaml        # observability, apps, argocd namespaces
│   ├── rbac.yaml              # RBAC for OTel collector, Argo CD
│   ├── network-policies.yaml  # Default-deny + explicit allow rules
│   └── secrets.yaml           # Secret templates (Grafana admin, etc.)
├── memory/                    # Qdrant vector memory for RAG
│   ├── __init__.py
│   └── qdrant_client.py       # Client, embedder, collection mgmt, store/retrieve
├── observability/             # Helm values + PrometheusRule CRDs
│   ├── alert-rules.yaml       # PodCrashLooping, HighMemoryUsage, PodOOMKilled, NodeNotReady
│   ├── loki-values.yaml       # Loki + Promtail Helm values
│   ├── otel-collector-values.yaml  # OTel Collector DaemonSet config
│   ├── prometheus-values.yaml # kube-prometheus-stack Helm values
│   └── grafana-dashboards/    # (empty — dashboards via ConfigMap or Grafana UI)
├── policies/                  # OPA Rego policy
│   ├── remediation.rego       # Allow known actions, deny high-risk/protected-ns
│   └── remediation_test.rego  # OPA unit tests
├── sample-app/                # Fault-injection workload
│   ├── Dockerfile
│   ├── app.py                 # FastAPI with /crash, /leak-memory, /slow, /metrics
│   └── k8s-manifest.yaml      # Standalone manifest (canonical is gitops/manifests/)
├── tests/                     # Integration tests (require live kind cluster)
│   ├── conftest.py
│   ├── test_incident_replay.py
│   ├── test_selfheal.py       # Argo CD selfHeal drift-reversion test
│   └── test_state_durability.py
├── requirements.txt           # Pinned Python dependencies
├── sentinelops_checkpoints.db # SQLite checkpoint DB (created at runtime)
└── README.md                  # Project overview + quick start
```

## Directory Purposes

### `agents/` — Agent Orchestration Layer
- **Purpose**: LangGraph pipeline — triage → diagnosis → remediation → policy review
- **Contains**: 4 agent nodes, graph compiler, checkpointer, shared contracts, GitHub bridge
- **Key files**: 
  - `graph.py:60-100` — `build_graph()` constructs StateGraph with 4 nodes + SQLite checkpointer
  - `contracts.py:92-123` — `GraphState` TypedDict defines the pipeline state schema
  - `checkpointer_config.py:41-73` — `get_checkpointer()` returns `SqliteSaver` with `check_same_thread=False`

### `incident-normalizer/` — Alert Ingestion & Enrichment
- **Purpose**: Receive AlertManager webhooks, fetch Loki logs + Prometheus metrics, build Incident, dispatch pipeline
- **Contains**: FastAPI webhook server with bounded thread pool
- **Key files**: 
  - `webhook_server.py:228-299` — `/webhook` endpoint (skips resolved alerts)
  - `webhook_server.py:63-104` — `fetch_loki_logs()` LogQL query
  - `webhook_server.py:111-197` — `fetch_recent_metrics()` Prometheus instant queries

### `memory/` — Vector Memory (RAG)
- **Purpose**: Store resolved incidents; retrieve similar past incidents for diagnosis context
- **Contains**: Qdrant client wrapper with FastEmbed embedder (BAAI/bge-small-en-v1.5, 384-dim)
- **Key files**: 
  - `qdrant_client.py:66-83` — `init_collection()` creates collection if missing
  - `qdrant_client.py:99-146` — `store_incident()` embeds + upserts
  - `qdrant_client.py:151-187` — `retrieve_similar()` cosine similarity search

### `policies/` — OPA Policy Engine
- **Purpose**: Declarative guardrails on remediation actions
- **Contains**: Rego policy package `sentinelops.remediation` with allow/deny rules
- **Key files**: 
  - `remediation.rego:19-26` — `allow` rule: known action ∧ risk≠high ∧ namespace not protected
  - `remediation.rego:29-38` — `known_actions` set: patch_memory_limit, patch_cpu_limit, scale_replicas, add_restart_annotation
  - `remediation.rego:41-47` — Protected namespaces: kube-system, kube-public, kube-node-lease, argocd, cert-manager

### `gitops/` — GitOps Reconciliation
- **Purpose**: Argo CD watches this directory; PR merges trigger cluster sync
- **Contains**: Argo CD Application manifest + canonical workload manifests
- **Key files**: 
  - `argocd-app.yaml:43-49` — `syncPolicy.automated: {prune: true, selfHeal: true}`
  - `manifests/sample-app-deployment.yaml:26` — `replicas: 1` (Git source of truth)

### `observability/` — Observability Stack Config
- **Purpose**: Helm values for Prometheus, Loki, OTel Collector; alert rules
- **Contains**: Helm values YAML + PrometheusRule CRD
- **Key files**: 
  - `prometheus-values.yaml:35-56` — AlertManager webhook config → `host.docker.internal:8000/webhook`
  - `alert-rules.yaml:18-30` — `PodCrashLooping` (2m), `HighMemoryUsage` (3m, >90%), `PodOOMKilled` (immediate)
  - `otel-collector-values.yaml:29-94` — kubeletstats receiver + k8sattributes processor + prometheusremotewrite exporter

### `infra/` — Cluster Provisioning
- **Purpose**: Kind cluster config, namespaces, RBAC, network policies
- **Contains**: Kind config, namespace definitions, RBAC, default-deny network policies
- **Key files**: 
  - `kind-config.yaml:3-6` — 3 nodes (control-plane + 2 workers)
  - `network-policies.yaml:8-17` — Traffic flow map (AlertManager→Normalizer, Normalizer→Loki/Prometheus, Agent→Qdrant/OPA/Ollama/GitHub)

### `sample-app/` — Fault Injection Workload
- **Purpose**: Generate realistic incidents for end-to-end validation
- **Contains**: FastAPI app with crash/memory-leak/latency endpoints + Prometheus metrics
- **Key files**: 
  - `app.py:41-45` — `/crash` → `os._exit(1)` (pod restart)
  - `app.py:48-58` — `/leak-memory` → allocates 50MB/call (3× > 128Mi limit → OOMKill)
  - `app.py:61-66` — `/slow` → `time.sleep(10)` (latency alert)

### `tests/` — Integration Tests
- **Purpose**: Validate Argo CD selfHeal, pipeline state durability, incident replay
- **Contains**: Pytest tests requiring live kind cluster + Argo CD
- **Key files**: 
  - `test_selfheal.py:104-150` — Scale to 5 replicas, assert Argo CD reverts to 1 within 300s
  - `test_state_durability.py` — Pipeline resumes from checkpoint after interruption

## Key File Locations

### Entry Points
- **Incident Normalizer**: `incident-normalizer/webhook_server.py:39` — `app = FastAPI(...)` → `uvicorn incident-normalizer.webhook_server:app --port 8000`
- **LangGraph Pipeline**: `agents/graph.py:110` — `get_graph()` → `graph.invoke({"incident": ...}, config={"configurable": {"thread_id": ...}})`
- **Argo CD Application**: `gitops/argocd-app.yaml` — `kubectl apply -f gitops/argocd-app.yaml`

### Configuration
- **Python deps**: `requirements.txt` — pinned versions (langgraph==1.2.9, qdrant-client==1.18.0, fastembed==0.8.0, etc.)
- **Environment variables** (see `docs/runbook.md:425-435`):
  - `GITHUB_TOKEN`, `GITHUB_REPO` — required for PR creation
  - `LOKI_URL`, `PROMETHEUS_URL`, `OPA_URL`, `QDRANT_URL` — service endpoints
  - `SENTINELOPS_CHECKPOINT_DB` — SQLite path (default: `sentinelops_checkpoints.db`)
  - `OLLAMA_MODEL` — LLM model (default: `qwen3-coder:latest`)
  - `AUTO_MERGE_LOW_RISK` — `true`/`false` (default: false)
  - `SLACK_WEBHOOK_URL` — optional rejection notifications

### Core Logic
- **Graph compilation**: `agents/graph.py:60-100` — `build_graph()`
- **State schema**: `agents/contracts.py:92-123` — `GraphState` TypedDict
- **Checkpoint persistence**: `agents/checkpointer_config.py:41-73` — `get_checkpointer()`
- **RCA with RAG**: `agents/diagnosis_agent.py:53-171` — `diagnosis_agent()`
- **Patch proposal**: `agents/remediation_agent.py:67-142` — `remediation_agent()`
- **Policy + PR**: `agents/policy_review_agent.py:113-207` — `policy_review_agent()`

### Testing
- **Run all**: `pytest tests/ -v`
- **Self-heal test**: `pytest tests/test_selfheal.py -v -s --timeout=300` (requires live cluster)
- **Unit-style pipeline test**: `tests/test_incident_replay.py`
- **Checkpoint durability**: `tests/test_state_durability.py`

## Naming Conventions

### Files
- **Python modules**: `snake_case.py` — `triage_agent.py`, `gitops_bridge.py`, `qdrant_client.py`
- **YAML manifests**: `kebab-case.yaml` — `argocd-app.yaml`, `sample-app-deployment.yaml`
- **Rego policies**: `snake_case.rego` — `remediation.rego`, `remediation_test.rego`
- **Helm values**: `kebab-case-values.yaml` — `prometheus-values.yaml`, `otel-collector-values.yaml`
- **Markdown docs**: `kebab-case.md` — `architecture.md`, `runbook.md`

### Directories
- **Packages**: `snake_case/` — `incident-normalizer/`, `gitops/manifests/`
- **Config groups**: `kebab-case/` — `observability/`, `infra/`

### Code Symbols
- **Classes**: `PascalCase` — `Incident`, `GraphState`, `GitOpsChange`, `RCAResult`
- **Functions**: `snake_case` — `triage_agent`, `diagnosis_agent`, `open_remediation_pr`, `retrieve_similar`
- **Constants**: `UPPER_SNAKE_CASE` — `VECTOR_DIM`, `COLLECTION`, `MODEL`, `RISK_LEVELS`
- **Environment vars**: `UPPER_SNAKE_CASE` — `GITHUB_TOKEN`, `LOKI_URL`, `SENTINELOPS_CHECKPOINT_DB`

## Where to Add New Code

### New Agent Node
1. Create `agents/<name>_agent.py` with `def <name>_agent(state: dict) -> dict:`
2. Add node to `agents/graph.py:83-86` — `workflow.add_node("<name>", <name>_agent)`
3. Add edge in `agents/graph.py:90-93` — `workflow.add_edge("<prev>", "<name>")`
4. Define new state keys in `agents/contracts.py:92-123` — `GraphState` TypedDict

### New Remediation Action Type
1. Add to `policies/remediation.rego:29-34` — `known_actions` set
2. Add risk level in `agents/remediation_agent.py:56-64` — `RISK_LEVELS` dict
3. Update `diagnosis_agent` SYSTEM_PROMPT if new evidence needed

### New Alert Rule
1. Add PrometheusRule to `observability/alert-rules.yaml:13-63` — new rule in `sentinelops.pod-health` or new group
2. Ensure label `release: kube-prometheus` matches `prometheus-values.yaml:18`
3. Alert labels must include `pod`, `namespace`, `severity` for Normalizer parsing

### New Observability Component
1. Add Helm values to `observability/<component>-values.yaml`
2. Deploy via `helm install` in quick-start (README.md:46-62)
3. Update network policies in `infra/network-policies.yaml` for new traffic flows

### New GitOps Manifest
1. Add YAML to `gitops/manifests/`
2. Argo CD auto-discovers (watches `path: gitops/manifests`)
3. Ensure `managed-by: sentinelops` label for identification

## Special Directories

### `sentinelops_checkpoints.db` (runtime-generated)
- **Purpose**: SQLite checkpoint database for LangGraph durability
- **Generated**: Yes — created by `SqliteSaver` on first pipeline invocation
- **Committed**: No — in `.gitignore` (not tracked)
- **Location**: Repo root (working directory when pipeline runs)

### `qdrant_storage/` (runtime-generated, Docker volume)
- **Purpose**: Qdrant persistent vector storage
- **Generated**: Yes — `docker run -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant`
- **Committed**: No — in `.gitignore`

### `__pycache__/` (Python bytecode)
- **Purpose**: Compiled `.pyc` files
- **Generated**: Yes — on module import
- **Committed**: No — in `.gitignore`

---

*Structure analysis: 2026-07-24*