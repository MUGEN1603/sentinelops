# SentinelOps 🚨

**Autonomous AIOps Incident Response Platform on Kubernetes**

SentinelOps detects Kubernetes incidents, enriches them with metrics/logs/traces, performs root-cause analysis using a multi-agent LLM pipeline with RAG memory, and applies remediations **only** through a policy-gated GitOps workflow. Git is the single path to cluster state change.

---

## Architecture

```
Alert fired ──► Incident Normalizer ──► LangGraph Pipeline ──► OPA Policy Gate
                                              │                        │
                                         Qdrant RAG           allowed ──► GitHub PR ──► Argo CD sync ──► Cluster healed
                                                              denied  ──► Log + Notify
```

## Stack

| Component | Technology |
|---|---|
| Cluster | `kind` (3-node local Kubernetes) |
| Metrics | Prometheus + AlertManager |
| Logs | Loki + Promtail |
| Traces/Meta | OpenTelemetry Collector (DaemonSet) |
| Agent Orchestration | LangGraph (4-agent, SQLite checkpointed) |
| Vector Memory | Qdrant + FastEmbed (BAAI/bge-small-en-v1.5, 384-dim) |
| LLM | Ollama / qwen3-coder (local) |
| Policy Gate | Open Policy Agent (Rego) |
| GitOps | GitHub API + Argo CD (selfHeal enabled) |

## Quick Start

```bash
# Prerequisites
brew install kind kubectl helm k9s argocd
brew install --cask docker

pip install -r requirements.txt

# Local services
docker run -d -p 6333:6333 -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant
docker run -d -p 8181:8181 openpolicyagent/opa run --server --addr :8181
ollama pull qwen3-coder

# Build cluster
kind create cluster --name sentinelops --config infra/kind-config.yaml
kubectl apply -f infra/namespaces.yaml

# Deploy observability
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
helm install kube-prometheus prometheus-community/kube-prometheus-stack -n observability -f observability/prometheus-values.yaml
helm install loki grafana/loki-stack -n observability -f observability/loki-values.yaml --set promtail.enabled=true
helm install otel-collector open-telemetry/opentelemetry-collector -n observability -f observability/otel-collector-values.yaml --set mode=daemonset

# Deploy sample app
docker build -t sentinelops/sample-app:v1 sample-app/
kind load docker-image sentinelops/sample-app:v1 --name sentinelops
kubectl apply -f sample-app/k8s-manifest.yaml -n apps

# Start incident normalizer
uvicorn incident-normalizer.webhook_server:app --port 8000 --reload

# Run tests
python -m pytest tests/ -v
```

## Validation

See `docs/runbook.md` for the 12-point end-to-end validation checklist.

## Known Limitations

- Single-cluster, single-tenant reference implementation
- Local LLM (Ollama/Qwen3-coder) — RCA quality lower than hosted models
- OPA policies are a portfolio baseline, not production security posture
- Argo CD selfHeal sync interval is ~3 minutes (not instantaneous)

## CV Summary

> Built SentinelOps, an autonomous AIOps incident response platform on Kubernetes using a LangGraph multi-agent pipeline with durable state persistence, RAG-backed root-cause analysis via Qdrant, OPA policy-gated remediation approval, and GitOps-only reconciliation via Argo CD with self-healing drift correction — validated end to end with real fault-injection drills and measured MTTR.
