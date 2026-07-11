# SentinelOps — Architecture Reference

## System Architecture

SentinelOps is an autonomous AIOps incident response platform that detects, diagnoses, and remediates Kubernetes incidents without any human in the loop for low-risk changes. Every cluster state change flows through Git — never directly via `kubectl`.

---

## Architecture Diagram

```
                        ┌─────────────────────────────────────────────────────────────────┐
                        │                    KUBERNETES CLUSTER (kind, 3-node)              │
                        │                                                                   │
                        │  ┌──────────────┐   ┌───────────────────┐   ┌────────────────┐  │
                        │  │ Sample App   │   │ Prometheus Stack   │   │ OTel Collector │  │
                        │  │ FastAPI      │──▶│ + AlertManager     │   │ DaemonSet      │  │
                        │  │ /crash       │   │ kube-prom-stack    │   │ kubeletstats   │  │
                        │  │ /leak-memory │   │                    │   │ filelog        │  │
                        │  │ /slow        │   └────────┬───────────┘   │ k8sattributes  │  │
                        │  └──────┬───────┘            │               └───────┬────────┘  │
                        │         │ logs               │ alert webhook          │ metrics   │
                        │         ▼                    │                        │           │
                        │  ┌──────────────┐            │                        │           │
                        │  │ Loki+Promtail│            │                        │           │
                        │  │ log agg.     │            │                        │           │
                        │  └──────────────┘            │                        │           │
                        └─────────────────────────────┼────────────────────────┼───────────┘
                                                       │                        │
                                              AlertManager POST       OTel metrics/logs
                                                       │                        │
                                                       ▼                        ▼
                              ┌────────────────────────────────────────────────────────────┐
                              │              Incident Normalizer (FastAPI :8000)            │
                              │  • Receives AlertManager webhook                            │
                              │  • Fetches Loki logs for affected pod                       │
                              │  • Fetches Prometheus metrics snapshot                      │
                              │  • Builds typed Incident{} object                           │
                              │  • Dispatches to LangGraph pipeline (async thread)          │
                              └───────────────────────────┬────────────────────────────────┘
                                                          │
                                                          ▼
                              ┌────────────────────────────────────────────────────────────┐
                              │                  LangGraph Agent Pipeline                   │
                              │       (SQLite checkpointer — durable, resumable state)      │
                              │                                                              │
                              │  ┌─────────────┐    ┌──────────────────┐                   │
                              │  │ triage_agent│    │  diagnosis_agent  │                   │
                              │  │             │───▶│                   │◀─── Qdrant RAG    │
                              │  │ Classify:   │    │ Root Cause Anal.  │     (384-dim       │
                              │  │ critical /  │    │ + similar history │      COSINE)       │
                              │  │ warning /   │    │                   │                   │
                              │  │ info        │    └────────┬──────────┘                   │
                              │  └─────────────┘             │                               │
                              │                              ▼                               │
                              │                   ┌──────────────────┐                      │
                              │                   │remediation_agent │                      │
                              │                   │ YAML patch only  │                      │
                              │                   │ no shell commands│                      │
                              │                   └────────┬─────────┘                      │
                              │                            │                                 │
                              │                            ▼                                 │
                              │                 ┌──────────────────────┐                    │
                              │                 │  policy_review_agent │                    │
                              │                 │  ┌───────────────┐   │                    │
                              │                 │  │  OPA (Rego)   │   │                    │
                              │                 │  │  allow/deny   │   │                    │
                              │                 │  └──────┬────────┘   │                    │
                              │                 └─────────┼────────────┘                    │
                              └───────────────────────────┼─────────────────────────────────┘
                                                          │
                                        ┌─────────────────┴─────────────────┐
                                        │                                   │
                                   allowed=true                       allowed=false
                                        │                                   │
                                        ▼                                   ▼
                            ┌───────────────────────┐           ┌────────────────────┐
                            │  GitHub PR created     │           │  Rejection logged  │
                            │  auto-fix/<incident-id>│           │  Slack notified    │
                            │  branch + YAML diff    │           └────────────────────┘
                            └──────────┬────────────┘
                                       │ merge (manual or auto for risk_level=low)
                                       ▼
                            ┌───────────────────────┐
                            │     Argo CD sync       │
                            │  selfHeal: true        │
                            │  prune: true           │
                            └──────────┬────────────┘
                                       │
                                       ▼
                            ┌───────────────────────┐
                            │  Cluster reconciled    │
                            │  Incident resolved     │
                            │  MTTR recorded         │
                            └───────────────────────┘
```

---

## Component Deep-Dives

### 1. kind Cluster (3-node)
- 1 control-plane, 2 workers
- All workloads distributed across workers
- `imagePullPolicy: Never` — images pre-loaded via `kind load docker-image`

### 2. Prometheus + AlertManager
- Deployed via `kube-prometheus-stack` Helm chart
- AlertManager webhook receiver points to Incident Normalizer at `host.docker.internal:8000/webhook`
- PrometheusRule CRDs picked up from all namespaces with label `release: kube-prometheus`
- Alert rules: `PodCrashLooping` (2m), `HighMemoryUsage >90%` (3m), `PodOOMKilled` (immediate)

### 3. Loki + Promtail
- Promtail runs as DaemonSet on every node, tailing `/var/log/pods/*/*/*.log`
- Log streams labeled with `namespace`, `pod`, `container` for easy Loki querying
- Incident Normalizer queries Loki via `GET /loki/api/v1/query_range` with LogQL

### 4. OTel Collector (DaemonSet)
- Receives: `kubeletstats` (node/pod/container metrics), `filelog` (pod log files)
- Processes: `k8sattributes` enriches every signal with k8s namespace/pod/container/node metadata
- Exports: `logging` exporter (stdout) for local dev — swap to OTLP for Tempo/Jaeger in production

### 5. Sample App (FastAPI)
- 4 fault-injection endpoints: `/healthy`, `/crash`, `/leak-memory`, `/slow`
- Memory limit: 128Mi — calling `/leak-memory` 3× triggers OOMKill (3 × 50MB = 150MB > 128Mi)
- Prometheus metrics via `prometheus_client`: request counters, memory gauge
- `imagePullPolicy: Never` — loaded via `kind load docker-image`

### 6. Incident Normalizer
- FastAPI webhook server on port 8000
- Receives AlertManager POST, skips resolved alerts
- Fetches Loki logs and Prometheus metrics for the affected pod
- Builds typed `Incident` Pydantic object
- Dispatches to LangGraph pipeline in a background thread (non-blocking response)

### 7. LangGraph Multi-Agent Pipeline

```
triage_agent → diagnosis_agent → remediation_agent → policy_review_agent → END
```

| Agent | Input | Output | LLM Role |
|---|---|---|---|
| `triage_agent` | Incident dict | `severity_classification`, `triage_summary` | Classify severity in JSON |
| `diagnosis_agent` | Incident + Qdrant similar incidents | `rca`, `similar_incidents`, `rca_confidence` | RCA with historical context |
| `remediation_agent` | Incident + RCA | `proposed_patch`, `patch_type`, `risk_level`, `rollback_hint` | YAML patch (no shell) |
| `policy_review_agent` | Patch + risk_level + namespace | `policy_allowed`, `policy_reasons`, `gitops_change` | OPA evaluation + PR creation |

**Durability**: SQLite checkpointer (`sentinelops_checkpoints.db`) persists every node completion. Re-invoking with the same `thread_id` resumes from the last checkpoint.

### 8. Qdrant Vector Memory
- Collection: `incidents`, dimension: 384, distance: COSINE
- Embedding model: `BAAI/bge-small-en-v1.5` via FastEmbed (~25MB, cached)
- `store_incident()`: called after pipeline completion to persist resolved incidents
- `retrieve_similar()`: called by `diagnosis_agent` to find top-3 similar past incidents
- **Critical**: changing the embedding model requires `recreate_collection()` — dimensions must match

### 9. OPA Policy Engine
- Runs as Docker container on port 8181
- Policy: `package sentinelops.remediation`
- `allow = true` iff: known action type, risk_level ≠ high, namespace not in protected set
- Protected namespaces: `kube-system`, `kube-public`, `kube-node-lease`, `argocd`, `cert-manager`
- **Fail closed**: if OPA is unreachable, `policy_review_agent` defaults to `allow=false`

### 10. GitHub PR Bridge
- Creates branch `auto-fix/<incident-id>` from main
- Commits the patched manifest to `gitops/manifests/sample-app-deployment.yaml`
- Opens a PR with full audit trail: incident ID, risk level, RCA summary, rollback instructions
- Optional auto-merge for `risk_level=low` (disabled by default — set `AUTO_MERGE_LOW_RISK=true`)

### 11. Argo CD (GitOps Reconciler)
- Watches `gitops/manifests/` directory on `main` branch
- `selfHeal: true` — reverts any manual cluster drift within the sync interval (~3 min)
- `prune: true` — removes cluster resources deleted from Git
- `CreateNamespace=true` — creates `apps` namespace if missing

---

## Data Flow: Alert → Resolution

```
T+0m    /leak-memory called 3× → OOMKill → Pod restarts
T+0m    Promtail captures OOMKill logs → pushes to Loki
T+0m    Kubelet metrics spike → Prometheus scrapes memory_usage > 90%
T+3m    HighMemoryUsage alert fires → AlertManager POSTs to Incident Normalizer
T+3m    Incident Normalizer builds Incident{} → dispatches to LangGraph
T+3m    triage_agent classifies: severity=critical
T+3m    diagnosis_agent queries Qdrant → retrieves similar incidents → LLM RCA
T+4m    remediation_agent proposes: patch_memory_limit, risk_level=medium
T+4m    policy_review_agent → OPA allow=true → GitHub PR created
T+Xm    Human reviews PR → merges (or auto-merged if risk_level=low)
T+Xm    Argo CD detects main branch change → syncs cluster
T+X+3m  Cluster reconciled with new memory limit → incident resolved
T+X+3m  MTTR recorded in runbook.md
```

---

## Security Model

| Concern | Mitigation |
|---|---|
| No direct cluster mutations | All changes via GitHub PR → Argo CD reconciliation |
| Unsafe remediations blocked | OPA Rego policy: deny high-risk, deny protected namespaces |
| LLM shell command injection | `remediation_agent` rejects any patch containing kubectl/bash/exec |
| OPA unreachable | Fail-closed: default deny if OPA returns an error |
| GitHub token exposure | Read from `GITHUB_TOKEN` env var — never hardcoded |
| High-risk PRs | Always require manual review — auto-merge only for `risk_level=low` |

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| Single-cluster, single-tenant | Not a multi-cluster fleet manager | Documented scope for portfolio project |
| Local LLM (qwen3-coder) quality | Lower RCA accuracy vs GPT-4 | Benchmark and report real MTTR numbers |
| OPA baseline policies | Not production security posture | Document as starting point; extend for production |
| FastEmbed 384-dim model | Speed over max retrieval accuracy | Documented, acceptable trade-off |
| Argo CD 3-min sync interval | selfHeal not instantaneous | Expected behavior — trigger manual sync for urgency |
| SQLite checkpointer | Single-process only; no horizontal scale | Swap to PostgresSaver for multi-instance deployment |
