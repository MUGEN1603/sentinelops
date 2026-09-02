# Technology Stack

**Analysis Date:** 2026-07-24

## Languages

**Primary:**
- Python 3.14.3 - Core agent pipeline, incident normalizer, sample app, tests
- YAML - Kubernetes manifests, Helm values, GitHub Actions workflows, OPA policies
- Rego - OPA policy definitions for remediation gating

**Secondary:**
- Dockerfile - Container images for sample-app, incident-normalizer
- Shell (bash) - CI/CD scripts, kind cluster setup, quickstart scripts

## Runtime

**Environment:**
- Python 3.14.3 (virtualenv at `.venv/`)
- kind (Kubernetes in Docker) v0.27.0 - Local 3-node cluster
- Docker - Container runtime for kind, images, and local services

**Package Manager:**
- pip 24.x with requirements.txt (pinned dependencies)
- Lockfile: requirements.txt (pinned versions, no lockfile separate)

## Frameworks

**Core:**
- LangGraph 1.2.9 - Multi-agent orchestration framework with SQLite checkpointing
- langgraph-checkpoint-sqlite 3.1.0 - Durable state persistence for agent graph
- langchain-community 0.4.2 - LangChain integrations (base interfaces)
- FastAPI 0.139.2 - Incident normalizer webhook server
- uvicorn 0.51.0 - ASGI server for FastAPI
- Pydantic 2.13.4 - Data validation and serialization (Incident, RCA, GitOps contracts)

**AI/LLM:**
- Ollama 0.6.2 - Local LLM runtime (Python client)
- Model: qwen3-coder (latest via Ollama) - Local inference for triage, diagnosis, remediation agents

**Vector Memory:**
- Qdrant Client 1.18.0 - Vector database client
- fastembed 0.8.0 - Embedding model (BAAI/bge-small-en-v1.5, 384-dim) via ONNX

**Kubernetes/Infrastructure:**
- kubernetes 36.0.3 - Python K8s client for Argo CD self-heal tests, incident enrichment
- PyGithub 2.9.1 - GitHub API for PR creation in policy_review_agent
- Prometheus Client 0.25.0 - Metrics exposition for sample-app

**Testing/Quality:**
- pytest 9.1.1 - Test runner
- pytest-timeout 2.4.0 - Test timeouts
- pytest-cov 6.0.0 - Coverage reporting
- httpx 0.28.1 - Async HTTP client for tests
- ruff - Linting (installed in CI)
- mypy - Type checking (installed in CI, run with --ignore-missing-imports)
- yamllint 1.35.1 - YAML linting
- trufflehog - Secret scanning (CI)

**Build/Dev:**
- docker/build-push-action v5 - Docker image builds in CI
- kind - Local Kubernetes cluster
- Helm - Package manager for observability stack
- Argo CD - GitOps continuous delivery

## Key Dependencies

**Critical:**
| Package | Version | Purpose |
|---------|---------|---------|
| langgraph | 1.2.9 | Agent graph orchestration with checkpointing |
| langgraph-checkpoint-sqlite | 3.1.0 | SQLite-backed durable state |
| ollama | 0.6.2 | Local LLM inference client |
| qdrant-client | 1.18.0 | Vector database for RAG memory |
| fastembed | 0.8.0 | Local embeddings (BAAI/bge-small-en-v1.5) |
| fastapi | 0.139.2 | Webhook server framework |
| kubernetes | 36.0.3 | K8s API client for enrichment & tests |
| PyGithub | 2.9.1 | GitHub PR creation for GitOps |
| prometheus-client | 0.25.0 | Metrics exposition |

**Infrastructure:**
| Package | Version | Purpose |
|---------|---------|---------|
| python-dotenv | 1.2.2 | Environment variable loading |
| requests | 2.34.2 | HTTP calls to Loki, Prometheus, OPA |

## Configuration

**Environment Variables (required at runtime):**
| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_MODEL` | LLM model tag | `qwen3-coder:latest` |
| `QDRANT_URL` | Qdrant endpoint | `http://localhost:6333` |
| `LOKI_URL` | Loki log query endpoint | `http://localhost:3100` |
| `PROMETHEUS_URL` | Prometheus query endpoint | `http://localhost:9090` |
| `OPA_URL` | OPA policy server | `http://localhost:8181` |
| `GITHUB_TOKEN` | GitHub PAT (repo:write) | Required for PR creation |
| `GITHUB_REPO` | Target repo `owner/repo` | Required for PR creation |
| `GITHUB_BASE_BRANCH` | Base branch for PRs | `main` |
| `SENTINELOPS_CHECKPOINT_DB` | SQLite checkpoint path | `sentinelops_checkpoints.db` |
| `AUTO_MERGE_LOW_RISK` | Auto-merge low-risk PRs | `false` |
| `PIPELINE_WORKERS` | Concurrent pipeline workers | `2` |
| `LOKI_LOG_MINUTES` | Log lookback window | `5` |
| `LOKI_LOG_LIMIT` | Max log lines per query | `100` |

**Build Config Files:**
- `requirements.txt` - Pinned Python dependencies
- `.github/workflows/ci.yaml` - CI/CD pipeline
- `.yamllint.yaml` - YAML linting rules
- `infra/kind-config.yaml` - Kind cluster config (3 nodes)
- `observability/*.yaml` - Helm values for Prometheus, Loki, OTEL Collector
- `gitops/argocd-app.yaml` - Argo CD Application manifest

## Platform Requirements

**Development:**
- macOS/Linux (kind requires Docker)
- Docker Desktop / Docker Engine
- Python 3.12+ (3.14.3 tested)
- 8GB+ RAM recommended (kind + Ollama + Qdrant)
- `brew install kind kubectl helm k9s argocd` (macOS)

**Production Target:**
- Kubernetes cluster (any CNCF-certified)
- Argo CD installed with `selfHeal: true`
- Prometheus + AlertManager (kube-prometheus-stack)
- Loki + Promtail (Grafana Loki stack)
- OpenTelemetry Collector (DaemonSet)
- Qdrant (vector DB) - can be deployed in-cluster
- OPA (policy server) - can be deployed in-cluster
- Ollama or hosted LLM endpoint (OpenAI/Azure compatible)
- GitHub repository with Actions enabled

---

*Stack analysis: 2026-07-24*