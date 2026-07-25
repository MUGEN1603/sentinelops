## SentinelOps Local Development Makefile
## Run `make help` to see available targets.

.PHONY: help up down status test test-unit test-e2e port-forward opa-load

VENV       := .venv/bin/python
PYTEST     := .venv/bin/python -m pytest
NAMESPACE_OBS := observability
NAMESPACE_APP := apps

# ─────────────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "SentinelOps — Local Stack Management"
	@echo "────────────────────────────────────────"
	@echo "  make up            Start Qdrant + OPA containers; load OPA policy"
	@echo "  make down          Stop and remove Qdrant + OPA containers"
	@echo "  make status        Show full cluster + service status"
	@echo "  make port-forward  Forward Prometheus (9090) and Loki (3100) to localhost"
	@echo "  make opa-load      Load/reload the remediation OPA policy"
	@echo "  make test          Run all unit tests with coverage"
	@echo "  make test-unit     Run fast unit tests only (no live services needed)"
	@echo ""

# ─────────────────────────────────────────────────────────────────────────────
up: _qdrant _opa opa-load
	@echo "✅ Qdrant (localhost:6333) and OPA (localhost:8181) are running"

_qdrant:
	@docker ps --format '{{.Names}}' | grep -q '^qdrant$$' && \
		echo "  qdrant: already running" || \
		(docker run -d --name qdrant -p 6333:6333 \
			-v "$(PWD)/qdrant_storage:/qdrant/storage" \
			qdrant/qdrant && echo "  qdrant: started")

_opa:
	@docker ps --format '{{.Names}}' | grep -q '^opa$$' && \
		echo "  opa:    already running" || \
		(docker run -d --name opa -p 8181:8181 \
			openpolicyagent/opa run --server --addr :8181 && echo "  opa:    started")
	@sleep 2

opa-load:
	@echo "  Loading OPA remediation policy..."
	@curl -sf -X PUT --data-binary @policies/remediation.rego \
		http://localhost:8181/v1/policies/remediation && \
		echo "  ✅ Policy loaded" || echo "  ❌ Failed to load policy (is OPA running?)"

down:
	@docker rm -f qdrant opa 2>/dev/null || true
	@echo "Qdrant and OPA stopped"

# ─────────────────────────────────────────────────────────────────────────────
status:
	@echo ""
	@echo "=== Kubernetes Cluster ==="
	@kubectl get nodes 2>&1 || echo "  cluster not running"
	@echo ""
	@echo "=== Observability Stack ==="
	@kubectl get pods -n $(NAMESPACE_OBS) 2>&1 || true
	@echo ""
	@echo "=== Application Namespace ==="
	@kubectl get pods -n $(NAMESPACE_APP) 2>&1 || true
	@echo ""
	@echo "=== Argo CD ==="
	@kubectl get pods -n argocd 2>&1 | head -5 || true
	@echo ""
	@echo "=== Local Services ==="
	@curl -sf http://localhost:6333/collections > /dev/null && \
		echo "  Qdrant  (6333): ✅ running" || echo "  Qdrant  (6333): ❌ down"
	@curl -sf http://localhost:8181/v1/policies > /dev/null && \
		echo "  OPA     (8181): ✅ running" || echo "  OPA     (8181): ❌ down"
	@curl -sf http://localhost:11434/api/tags > /dev/null && \
		echo "  Ollama (11434): ✅ running" || echo "  Ollama (11434): ❌ down"
	@curl -sf http://localhost:9090/-/ready > /dev/null && \
		echo "  Prometheus(9090): ✅ port-forward active" || echo "  Prometheus(9090): ❌ not forwarded"
	@curl -sf http://localhost:3100/ready > /dev/null && \
		echo "  Loki    (3100): ✅ port-forward active" || echo "  Loki    (3100): ❌ not forwarded"
	@echo ""

# ─────────────────────────────────────────────────────────────────────────────
port-forward:
	@echo "Starting port-forwards (Ctrl-C to stop)..."
	@kubectl port-forward -n $(NAMESPACE_OBS) \
		svc/kube-prometheus-prometheus 9090:9090 &
	@kubectl port-forward -n $(NAMESPACE_OBS) \
		svc/loki 3100:3100 &
	@echo "  Prometheus → localhost:9090"
	@echo "  Loki       → localhost:3100"
	@wait

# ─────────────────────────────────────────────────────────────────────────────
test: test-unit
	@echo ""
	@echo "=== Coverage Summary ==="
	@$(PYTEST) tests/ -v --tb=short \
		--ignore=tests/test_selfheal.py \
		--ignore=tests/test_e2e_verification.py \
		--cov=agents --cov=memory \
		--cov-report=term-missing \
		--cov-fail-under=50 2>&1

test-unit:
	@$(PYTEST) tests/test_incident_replay.py tests/test_state_durability.py \
		-v --tb=short 2>&1
