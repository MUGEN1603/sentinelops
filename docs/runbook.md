# SentinelOps — Operations Runbook

This runbook is the official record of the SentinelOps end-to-end validation.
Each section corresponds to one step in the 12-point validation checklist.
Fill in the **Result** and **Timestamp** columns as you complete each step.

---

## Prerequisites

Before running validation, confirm these are satisfied:

| Item | Command | Expected |
|---|---|---|
| Docker Desktop running | `docker ps` | No error |
| Qdrant running | `curl http://localhost:6333/collections` | `{"result":{"collections":[...]},"status":"ok"}` |
| OPA running | `curl http://localhost:8181/v1/policies` | `{"result":{}}` |
| Ollama + qwen3-coder | `ollama list` | `qwen3-coder` appears |
| GitHub token set | `echo $GITHUB_TOKEN` | Non-empty string |
| GitHub repo set | `echo $GITHUB_REPO` | `username/sentinelops` |

---

## Validation Checklist

### ✅ Check 1 — Cluster Ready

**Command**:
```bash
kubectl get nodes
```

**Expected**:
```
NAME                        STATUS   ROLES           AGE
sentinelops-control-plane   Ready    control-plane   Xm
sentinelops-worker          Ready    <none>          Xm
sentinelops-worker2         Ready    <none>          Xm
```

| Field | Value |
|---|---|
| **Timestamp** | |
| **Result** | PASS / FAIL |
| **Notes** | |

---

### ✅ Check 2 — Observability Stack Running

**Command**:
```bash
kubectl get pods -n observability
```

**Expected**: All pods in `Running` state:
- `alertmanager-kube-prometheus-alertmanager-0`
- `kube-prometheus-grafana-*`
- `kube-prometheus-kube-state-metrics-*`
- `kube-prometheus-operator-*`
- `kube-prometheus-prometheus-0`
- `loki-0`
- `loki-promtail-*` (one per node)
- `otel-collector-*` (one per node)

| Field | Value |
|---|---|
| **Timestamp** | |
| **Result** | PASS / FAIL |
| **Pod count** | |
| **Any non-Running pods** | |

---

### ✅ Check 3 — Alert Fires Within 3 Minutes

**Commands**:
```bash
# Port-forward to sample-app
kubectl port-forward svc/sample-app 8080:8080 -n apps &

# Trigger memory leak (call 3-4 times to cross the 128Mi limit)
for i in 1 2 3 4; do
  curl http://localhost:8080/leak-memory
  sleep 2
done

# Watch pod restarts
kubectl get pods -n apps -w
```

**Verify alert in Prometheus UI**:
```bash
kubectl port-forward svc/kube-prometheus-prometheus 9090:9090 -n observability &
open http://localhost:9090/alerts
```

Expected: `PodCrashLooping` and/or `PodOOMKilled` show **Firing** within 3 minutes.

| Field | Value |
|---|---|
| **Alert fired timestamp** | |
| **Alerts fired** | PodCrashLooping / PodOOMKilled / HighMemoryUsage |
| **Result** | PASS / FAIL |

---

### ✅ Check 4 — Incident Normalizer Receives Webhook

**Start the normalizer**:
```bash
uvicorn "incident-normalizer.webhook_server:app" --port 8000 --reload
```

**Test manually with a mock payload**:
```bash
curl -X POST localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "version": "4",
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "PodCrashLooping",
        "pod": "sample-app-abc123",
        "namespace": "apps",
        "severity": "critical"
      },
      "startsAt": "2024-01-01T00:00:00Z"
    }]
  }'
```

**Expected response**:
```json
{"status": "processed", "count": 1}
```

| Field | Value |
|---|---|
| **Timestamp** | |
| **Response** | |
| **Log lines (incident id logged?)** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 5 — Full Pipeline Runs (RCA + Patch + Policy)

**Commands**:
```python
# Run from repo root
python -c "
from agents.graph import graph
import uuid

incident = {
    'id': str(uuid.uuid4()),
    'started_at': '2024-01-01T00:00:00Z',
    'namespace': 'apps',
    'workload': 'sample-app-abc123',
    'kind': 'Pod',
    'severity': 'critical',
    'alert_labels': {'alertname': 'PodCrashLooping', 'namespace': 'apps'},
    'recent_logs': ['ERROR: OOMKilled', 'container exceeded memory limit'],
    'recent_metrics': {'memory_usage_bytes': 134217728},
    'k8s_objects': [],
    'trace_refs': []
}

result = graph.invoke(
    {'incident': incident},
    config={'configurable': {'thread_id': 'manual-test-1'}}
)

print('severity_classification:', result.get('severity_classification'))
print('rca (first 200 chars):',   result.get('rca', '')[:200])
print('proposed_patch (first 200):', result.get('proposed_patch', '')[:200])
print('policy_allowed:',           result.get('policy_allowed'))
print('pr_url:',                   result.get('gitops_change', {}).get('pr_url'))
"
```

| Field | Value |
|---|---|
| **Timestamp** | |
| **severity_classification** | |
| **rca (summary)** | |
| **proposed_patch (type)** | |
| **policy_allowed** | true / false |
| **pr_url** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 6 — Durable State: Resume After Crash

**Test**:
```bash
# Run pipeline — kill it mid-way with Ctrl+C after seeing "Triage started" log
python -c "
from agents.graph import graph
result = graph.invoke(
    {'incident': <same incident dict as Check 5>},
    config={'configurable': {'thread_id': 'manual-test-1'}}  # SAME thread_id
)
print('Resumed state:', list(result.keys()))
"
```

Expected: The pipeline logs show it **resumed from diagnosis_agent** (not triage_agent).
The triage node's log message "Triage started" should NOT appear on the second run.

| Field | Value |
|---|---|
| **Timestamp** | |
| **Was triage re-executed?** | YES / NO (should be NO) |
| **Which node did it resume from?** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 7 — Qdrant RAG Retrieval Works

**Test**:
```python
python -c "
from memory.qdrant_client import init_collection, store_incident, retrieve_similar
init_collection()
store_incident('test-oom-1', 'OOMKill in payment-service namespace apps', 'memory leak in heap allocator', 'resolved')
results = retrieve_similar('OOMKill in checkout-service', k=3)
print('Results:', results)
assert len(results) > 0, 'No results returned!'
assert results[0]['score'] > 0.5, f'Similarity score too low: {results[0][\"score\"]}'
print('✓ Qdrant RAG working. Top score:', results[0]['score'])
"
```

| Field | Value |
|---|---|
| **Timestamp** | |
| **Top similarity score** | |
| **Returned incident ID** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 8 — OPA Blocks Unsafe Remediations

**Test allow case**:
```bash
curl -X POST localhost:8181/v1/data/sentinelops/remediation/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"patch_memory_limit","risk_level":"medium","namespace":"apps"}}'
# Expected: {"result":true}
```

**Test deny case (high risk)**:
```bash
curl -X POST localhost:8181/v1/data/sentinelops/remediation/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"patch_memory_limit","risk_level":"high","namespace":"apps"}}'
# Expected: {"result":false}
```

**Test deny case (protected namespace)**:
```bash
curl -X POST localhost:8181/v1/data/sentinelops/remediation/allow \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"patch_memory_limit","risk_level":"low","namespace":"kube-system"}}'
# Expected: {"result":false}
```

| Field | Value |
|---|---|
| **Timestamp** | |
| **medium/apps result** | true / false (should be true) |
| **high/apps result** | true / false (should be false) |
| **low/kube-system result** | true / false (should be false) |
| **Result** | PASS / FAIL |

---

### ✅ Check 9 — Real GitHub PR Created

**Trigger full pipeline on real /leak-memory incident** (Checks 3+4+5 must have passed first).

Confirm a PR appears at: `https://github.com/<your-username>/sentinelops/pulls`

Expected PR:
- Title: `🔧 Auto-remediation: incident <id> [medium risk]`
- Branch: `auto-fix/<incident-id>`
- Body contains: incident ID, RCA summary, risk level, rollback instructions
- File changed: `gitops/manifests/sample-app-deployment.yaml`

| Field | Value |
|---|---|
| **Timestamp** | |
| **PR URL** | |
| **Branch name** | |
| **File changed** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 10 — Argo CD Syncs After PR Merge

**Merge the PR from Check 9**, then:

```bash
# Watch Argo CD sync status
argocd app get sentinelops-app --refresh

# Or trigger manual sync
argocd app sync sentinelops-app

# Verify cluster state updated
kubectl get deployment sample-app -n apps -o json | jq '.spec.template.spec.containers[0].resources'
```

Expected: The cluster Deployment now has the updated memory limits from the merged PR.

| Field | Value |
|---|---|
| **Timestamp of merge** | |
| **Timestamp of sync** | |
| **New memory limit in cluster** | |
| **Result** | PASS / FAIL |

---

### ✅ Check 11 — Argo CD selfHeal Reverts Drift

```bash
# Introduce manual drift
kubectl scale deployment sample-app -n apps --replicas=5

# Watch Argo CD revert it (watch replicas every 10s)
watch -n 10 "kubectl get deployment sample-app -n apps -o jsonpath='{.spec.replicas}'"
```

Expected: Replicas revert to 1 (Git-defined) within 3 minutes.

```bash
# Alternative: trigger sync manually to speed this up
argocd app sync sentinelops-app
```

| Field | Value |
|---|---|
| **Timestamp drift introduced** | |
| **Timestamp reverted** | |
| **Time to revert** | seconds |
| **Result** | PASS / FAIL |

---

### ✅ Check 12 — MTTR Recorded

Calculate Mean Time To Resolution from the timestamps recorded above:

| Metric | Timestamp | 
|---|---|
| Alert fired (Check 3) | |
| Incident normalizer received (Check 4) | |
| Pipeline completed + PR created (Check 9) | |
| PR merged (Check 10) | |
| Cluster reconciled (Check 10) | |

**MTTR (alert to cluster-healed)**: __________ minutes

**MTTR Breakdown**:
- Alert detection lag: ______ min
- Pipeline execution time: ______ min  
- Human review + merge time: ______ min (0 if auto-merged)
- Argo CD sync lag: ______ min (~3 min)

---

## Known Issues & Troubleshooting

### AlertManager not sending webhooks

1. Check AlertManager config: `kubectl get secret alertmanager-kube-prometheus-alertmanager -n observability -o yaml`
2. Verify the URL is reachable from inside the cluster: `host.docker.internal:8000`
3. On Linux (not Docker Desktop): use the host's actual IP instead of `host.docker.internal`

### Qdrant insert fails with dimension mismatch

```
Error: Vector dimension 768 != 384
```

Solution: The embedding model was changed without recreating the collection.
```python
from memory.qdrant_client import recreate_collection
recreate_collection()  # WARNING: deletes all stored incidents
```

### LangGraph node not resuming

Check that:
1. `sentinelops_checkpoints.db` exists in the working directory
2. The `thread_id` in the second invocation matches exactly
3. `SENTINELOPS_CHECKPOINT_DB` env var is not pointing to a different file

### Argo CD not syncing

```bash
argocd app get sentinelops-app   # Check status
argocd app logs sentinelops-app  # Check logs
kubectl get pods -n argocd       # Check Argo CD health
```

Common causes:
- Repo not accessible (check GITHUB_TOKEN permissions)
- `sentinelops-app` Application not applied: `kubectl apply -f gitops/argocd-app.yaml`
- selfHeal not enabled: check `gitops/argocd-app.yaml` for `selfHeal: true`

---

## Environment Variables Reference

| Variable | Required | Description | Example |
|---|---|---|---|
| `GITHUB_TOKEN` | Yes (for PRs) | GitHub Personal Access Token (repo:write) | `ghp_xxxxx` |
| `GITHUB_REPO` | Yes (for PRs) | Repository in owner/repo format | `gauravpandey/sentinelops` |
| `LOKI_URL` | No | Loki HTTP API URL | `http://localhost:3100` |
| `PROMETHEUS_URL` | No | Prometheus HTTP API URL | `http://localhost:9090` |
| `OPA_URL` | No | OPA server URL | `http://localhost:8181` |
| `QDRANT_URL` | No | Qdrant HTTP URL | `http://localhost:6333` |
| `SENTINELOPS_CHECKPOINT_DB` | No | SQLite checkpoint DB path | `sentinelops_checkpoints.db` |
| `SLACK_WEBHOOK_URL` | No | Slack Incoming Webhook for rejection notifications | `https://hooks.slack.com/...` |
| `AUTO_MERGE_LOW_RISK` | No | Auto-merge PRs with risk_level=low | `false` |
