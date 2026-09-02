# External Integrations

**Analysis Date:** 2026-07-24

## APIs & External Services

**LLM Inference:**
- **Ollama** - Local LLM runtime
  - Model: `qwen3-coder` (or `qwen3-coder:latest`, `qwen3-coder:30b`)
  - Client: `ollama` Python package (v0.6.2)
  - Used by: `agents/triage_agent.py`, `agents/diagnosis_agent.py`, `agents/remediation_agent.py`
  - Auth: None (local HTTP on `http://localhost:11434`)

**Vector Database:**
- **Qdrant** - Vector similarity search for RAG
  - Client: `qdrant-client` (v1.18.0)
  - Collection: `incidents` (384-dim, cosine distance)
  - Embedding model: BAAI/bge-small-en-v1.5 via `fastembed` (v0.8.0)
  - URL: `QDRANT_URL` env var (default `http://localhost:6333`)
  - Used by: `memory/qdrant_client.py` → `agents/diagnosis_agent.py` (retrieve), `agents/policy_review_agent.py` (store)

**Policy Engine:**
- **OPA (Open Policy Agent)** - Rego policy evaluation
  - Server: `openpolicyagent/opa` container on port 8181
  - Endpoint: `POST /v1/data/sentinelops/remediation/allow`
  - Deny reasons: `POST /v1/data/sentinelops/remediation/deny_reason`
  - URL: `OPA_URL` env var (default `http://localhost:8181`)
  - Fail-closed: Connection errors → deny by default
  - Used by: `agents/policy_review_agent.py::query_opa()`

**GitOps / Source Control:**
- **GitHub API** - PR creation for remediation
  - Client: `PyGithub` (v2.9.1)
  - Auth: `GITHUB_TOKEN` env var (PAT with `repo:write` scope)
  - Repo: `GITHUB_REPO` env var (`owner/repo` format)
  - Base branch: `GITHUB_BASE_BRANCH` (default `main`)
  - Auto-merge: `AUTO_MERGE_LOW_RISK` (default `false`)
  - Used by: `agents/gitops_bridge.py::open_remediation_pr()`

**Notifications (Optional):**
- **Slack** - Rejection notifications
  - Webhook URL: `SLACK_WEBHOOK_URL` env var
  - Used by: `agents/policy_review_agent.py::notify_slack_rejection()`

## Data Storage

**Databases:**
- **SQLite** - LangGraph checkpoint persistence
  - File: `SENTINELOPS_CHECKPOINT_DB` (default `sentinelops_checkpoints.db`)
  - Driver: `langgraph-checkpoint-sqlite` (v3.1.0) via `SqliteSaver`
  - Connection: Direct `sqlite3` with `check_same_thread=False`
  - Used by: `agents/checkpointer_config.py::get_checkpointer()` → `agents/graph.py`

- **Qdrant** - Vector embeddings for incident memory
  - See Vector Database section above
  - Collection persists to `qdrant_storage/` volume

**File Storage:**
- Local filesystem only
- `qdrant_storage/` - Qdrant data directory (Docker volume)
- `sentinelops_checkpoints.db` - SQLite checkpoint file
- `gitops/manifests/` - Git-tracked Kubernetes manifests (source of truth)

**Caching:**
- None explicitly configured
- Qdrant embeddings cached in `~/.cache/fastembed/`
- Ollama models cached in `~/.ollama/models/`

## Authentication & Identity

**Auth Providers:**
- **GitHub PAT** - Primary auth for GitOps writes
  - Scope: `repo` (full repo access including write)
  - Stored in: `GITHUB_TOKEN` environment variable
  - Used by: `agents/gitops_bridge.py` via `PyGithub`

- **Kubernetes Service Account** - In-cluster API access
  - Used by: OpenTelemetry Collector (kubeletstats, k8sattributes)
  - Used by: Argo CD (reconciliation)
  - Tests: `kubernetes` Python client loads in-cluster or kubeconfig

- **Ollama** - No auth (local)

- **Qdrant** - No auth by default (local dev); can enable API key in production

- **OPA** - No auth (local dev); can enable OIDC in production

## Monitoring & Observability

**Error Tracking:**
- None configured (local dev)
- Structured logging via Python `logging` module
- Log levels: INFO (normal), WARNING (degraded), ERROR (failures)

**Logs:**
- **Loki** - Log aggregation (Grafana Loki stack)
  - URL: `LOKI_URL` env var (default `http://localhost:3100`)
  - Query: LogQL via `/loki/api/v1/query_range`
  - Used by: `incident-normalizer/webhook_server.py::fetch_loki_logs()`
  - Retention: Default (configured in Loki helm values)

- **OpenTelemetry Collector** - DaemonSet log collection
  - Receives: `/var/log/pods/*/*/*.log` via filelog receiver
  - Exports: stdout (logging exporter) for debug
  - K8s metadata enrichment via `k8sattributes` processor

**Metrics:**
- **Prometheus** - Metrics collection & alerting
  - URL: `PROMETHEUS_URL` env var (default `http://localhost:9090`)
  - Remote write receiver enabled for OTel collector
  - Queried by: `incident-normalizer/webhook_server.py::fetch_recent_metrics()`
  - Queries: `container_memory_usage_bytes`, `rate(container_cpu_usage_seconds_total[5m])`, `kube_pod_container_status_restarts_total`

- **kube-prometheus-stack** - Full monitoring stack
  - Prometheus, AlertManager, Grafana, kube-state-metrics, node-exporter
  - ServiceMonitors/PodMonitors for auto-discovery

**Tracing:**
- **OpenTelemetry Collector** - DaemonSet
  - Receivers: `kubeletstats` (metrics), `filelog` (logs)
  - Exporters: `prometheusremotewrite` (metrics → Prometheus), `logging` (debug)
  - Processors: `k8sattributes` (enrichment), `batch`, `memory_limiter`

**Alerting:**
- **AlertManager** - Routes alerts to SentinelOps webhook
  - Webhook receiver: `sentinelops-webhook` → `http://host.docker.internal:8000/webhook`
  - Config: `observability/prometheus-values.yaml` → `alertmanager.config.receivers`
  - Grouping: by `alertname`, `namespace`, `pod`

## CI/CD & Deployment

**Hosting:**
- Local development: `kind` (Kubernetes in Docker)
- CI: GitHub Actions runners (ubuntu-latest)
- Production target: Any CNCF-certified Kubernetes cluster

**CI Pipeline:** `.github/workflows/ci.yaml`
- Lint: `ruff`, `mypy`, `yamllint`
- Test: `pytest` with coverage
- Security: `trufflehog` secret scanning
- Build: Docker images for sample-app
- Kind cluster creation for integration tests

**CD / GitOps:**
- **Argo CD** - Continuous delivery
  - Application: `sentinelops-app` (watches `gitops/manifests/` on `main`)
  - Sync policy: `automated.prune=true`, `selfHeal=true` (mandatory)
  - Sync options: `CreateNamespace=true`, `PrunePropagationPolicy=foreground`
  - Self-heal reverts manual `kubectl scale`/`edit` drift within ~3 min

- **GitHub Actions** - PR automation
  - Auto-merge low-risk PRs (if `AUTO_MERGE_LOW_RISK=true`)

## Environment Configuration

**Required Environment Variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes (for PRs) | GitHub PAT with `repo:write` |
| `GITHUB_REPO` | Yes (for PRs) | Target repo `owner/repo` |
| `OLLAMA_MODEL` | No | LLM model tag (default: `qwen3-coder:latest`) |
| `QDRANT_URL` | No | Qdrant endpoint (default: `http://localhost:6333`) |
| `LOKI_URL` | No | Loki endpoint (default: `http://localhost:3100`) |
| `PROMETHEUS_URL` | No | Prometheus endpoint (default: `http://localhost:9090`) |
| `OPA_URL` | No | OPA server (default: `http://localhost:8181`) |
| `SENTINELOPS_CHECKPOINT_DB` | No | SQLite checkpoint path |
| `AUTO_MERGE_LOW_RISK` | No | Auto-merge low-risk PRs (default: `false`) |
| `PIPELINE_WORKERS` | No | Concurrent pipeline workers (default: `2`) |
| `LOKI_LOG_MINUTES` | No | Log lookback minutes (default: `5`) |
| `LOKI_LOG_LIMIT` | No | Max log lines (default: `100`) |
| `SLACK_WEBHOOK_URL` | No | Slack webhook for rejections |
| `GITHUB_BASE_BRANCH` | No | Base branch for PRs (default: `main`) |

**Secrets Location:**
- Local dev: Shell environment / `.env` file (loaded via `python-dotenv`)
- CI: GitHub Actions secrets (`GITHUB_TOKEN`, `GITHUB_REPO`, etc.)
- Production: Kubernetes Secrets / External Secrets Operator / SealedSecrets

**Config Files (committed):**
- `requirements.txt` - Python dependencies
- `observability/*.yaml` - Helm values for observability stack
- `gitops/argocd-app.yaml` - Argo CD Application (edit repoURL before apply)
- `gitops/manifests/sample-app-deployment.yaml` - Canonical Deployment manifest
- `.github/workflows/ci.yaml` - CI pipeline
- `.yamllint.yaml` - YAML lint rules

## Webhooks & Callbacks

**Incoming (to SentinelOps):**
- **AlertManager → Incident Normalizer**
  - Endpoint: `POST /webhook` on `incident-normalizer:8000`
  - Payload: AlertManager webhook format (v4)
  - Trigger: Firing alerts with `severity: critical|warning`
  - Config: `observability/prometheus-values.yaml` → `alertmanager.config.receivers[0].webhook_configs[0].url`

**Outgoing (from SentinelOps):**
- **Incident Normalizer → Loki**
  - `GET /loki/api/v1/query_range` (log queries)
  - Called by: `fetch_loki_logs()` in `webhook_server.py`

- **Incident Normalizer → Prometheus**
  - `GET /api/v1/query` (metric queries)
  - Called by: `fetch_recent_metrics()` in `webhook_server.py`

- **Policy Review Agent → OPA**
  - `POST /v1/data/sentinelops/remediation/allow` (policy decision)
  - `POST /v1/data/sentinelops/remediation/deny_reason` (deny reasons)
  - Called by: `query_opa()` in `policy_review_agent.py`

- **Policy Review Agent → GitHub API**
  - `POST /repos/{owner}/{repo}/git/refs` (create branch)
  - `PUT /repos/{owner}/{repo}/contents/{path}` (commit file)
  - `POST /repos/{owner}/{repo}/pulls` (create PR)
  - `PUT /repos/{owner}/{repo}/pulls/{number}/merge` (auto-merge)
  - Called by: `open_remediation_pr()` in `gitops_bridge.py`

- **Policy Review Agent → Qdrant**
  - `POST /collections/incidents/points` (upsert incident embedding)
  - Called by: `store_incident()` in `memory/qdrant_client.py` from `policy_review_agent.py`

- **Policy Review Agent → Slack (optional)**
  - `POST {SLACK_WEBHOOK_URL}` (rejection notification)
  - Called by: `notify_slack_rejection()` in `policy_review_agent.py`

- **OTel Collector → Prometheus**
  - `POST /api/v1/write` (remote write)
  - Configured in `otel-collector-values.yaml` exporters

---

*Integration audit: 2026-07-24*