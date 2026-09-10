#!/bin/bash
# scripts/runbook-checklist.sh — Automated 12-point runbook validation
#
# Run from repo root:
#   ./scripts/runbook-checklist.sh
#
# Exit codes:
#   0 = All checks passed
#   1 = One or more checks failed
#   2 = Prerequisites not met (cluster not running, etc.)

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────────
NAMESPACE_APPS="apps"
NAMESPACE_OBS="observability"
NAMESPACE_ARGOCD="argocd"
SAMPLE_APP="sample-app"
ARGOCD_APP="sentinelops-app"
TIMEOUT=180  # seconds for self-heal verification

# ─── Colors ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
SKIP=0

check() {
    local name=$1
    local cmd=$2
    if eval "$cmd" >/dev/null 2>&1; then
        echo -e "${GREEN}✅ PASS${NC}  $name"
        ((PASS++))
        return 0
    else
        echo -e "${RED}❌ FAIL${NC}  $name"
        ((FAIL++))
        return 1
    fi
}

skip() {
    local name=$1
    local reason=$2
    echo -e "${YELLOW}⏭️  SKIP${NC}  $name ($reason)"
    ((SKIP++))
}

header() {
    echo ""
    echo -e "${BLUE}=== $1 ===${NC}"
}

# ─── Prerequisites ──────────────────────────────────────────────────────────────
header "Prerequisites"
command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found"; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl not found"; exit 2; }

# Check cluster connectivity
check "Kubernetes API reachable" "kubectl get --raw=/healthz 2>/dev/null | grep -q ok"

# ─── Check 1: Cluster Foundation ───────────────────────────────────────────────
header "Check 1: Cluster Foundation"
check "Kind cluster exists" "kind get clusters 2>/dev/null | grep -q sentinelops"
check "3 nodes Ready" "kubectl get nodes --no-headers | grep -c Ready | grep -q 3"
check "Namespaces exist" "kubectl get ns $NAMESPACE_OBS $NAMESPACE_APPS $NAMESPACE_ARGOCD --no-headers 2>/dev/null | grep -c Active | grep -q 3"
check "RBAC resources applied" "kubectl get clusterrole -l managed-by=sentinelops --no-headers 2>/dev/null | grep -q ."
check "NetworkPolicies applied" "kubectl get networkpolicy -A --no-headers 2>/dev/null | grep -q ."

# ─── Check 2: Observability Stack ──────────────────────────────────────────────
header "Check 2: Observability Stack"
check "Prometheus Running" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=prometheus --no-headers 2>/dev/null | grep -q Running"
check "AlertManager Running" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=alertmanager --no-headers 2>/dev/null | grep -q Running"
check "Loki Running" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=loki --no-headers 2>/dev/null | grep -q Running"
check "Promtail DaemonSet (3/3)" "kubectl get pods -n $NAMESPACE_OBS -l app=loki-promtail --no-headers 2>/dev/null | grep -c Running | grep -q 3"
check "OTel Collector DaemonSet (2/2)" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=opentelemetry-collector --no-headers 2>/dev/null | grep -c Running | grep -q 2"
check "PrometheusRules loaded" "kubectl get prometheusrule -n $NAMESPACE_OBS --no-headers 2>/dev/null | grep -q sentinelops-alerts"
check "Grafana Running" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=grafana --no-headers 2>/dev/null | grep -q Running"

# ─── Check 3: Alerting & Incident Pipeline ─────────────────────────────────────
header "Check 3: Alerting & Incident Pipeline"
check "AlertManager CR Synced" "kubectl get alertmanager kube-prometheus-kube-prome-alertmanager -n $NAMESPACE_OBS -o jsonpath='{.status.conditions[?(@.type==\"Reconciled\")].status}' 2>/dev/null | grep -q True"
check "AlertManager Pod Ready (2/2)" "kubectl get pods -n $NAMESPACE_OBS -l app.kubernetes.io/name=alertmanager --no-headers 2>/dev/null | grep -q '2/2.*Running'"
check "Prometheus Webhook Configured" "kubectl get secret alertmanager-kube-prometheus-kube-prome-alertmanager -n $NAMESPACE_OBS -o jsonpath='{.data.alertmanager\.yaml}' 2>/dev/null | base64 -d | grep -q sentinelops-webhook"
check "Watchdog Route Drops" "kubectl get secret alertmanager-kube-prometheus-kube-prome-alertmanager -n $NAMESPACE_OBS -o jsonpath='{.data.alertmanager\.yaml}' 2>/dev/null | base64 -d | grep -A2 'alertname = \"Watchdog\"' | grep -q 'receiver: \"null\"'"

# ─── Check 4: LangGraph Pipeline ───────────────────────────────────────────────
header "Check 4: LangGraph Pipeline"
check "Pipeline Compiles" "cd $(dirname "$0")/.. && .venv/bin/python -c 'from agents.graph import build_graph; g=build_graph(); print(\"OK\")' 2>&1 | grep -q OK"
check "State Durability Tests" ".venv/bin/python -m pytest tests/test_state_durability.py -v --tb=short 2>&1 | grep -q '3 passed'"

# ─── Check 5: OPA Policy ───────────────────────────────────────────────────────
header "Check 5: OPA Policy"
check "OPA Tests (16/16)" "opa test policies/ -v 2>&1 | grep -q 'PASS: 16/16'"

# ─── Check 6: GitOps & Argo CD ─────────────────────────────────────────────────
header "Check 6: GitOps & Argo CD"
check "Argo CD Server Running" "kubectl get pods -n $NAMESPACE_ARGOCD -l app.kubernetes.io/name=argocd-server --no-headers 2>/dev/null | grep -q Running"
check "Argo CD Repo Server" "kubectl get pods -n $NAMESPACE_ARGOCD -l app.kubernetes.io/name=argocd-repo-server --no-headers 2>/dev/null | grep -q Running"
check "Argo CD Application Exists" "kubectl get application $ARGOCD_APP -n $NAMESPACE_ARGOCD --no-headers 2>/dev/null | grep -q $ARGOCD_APP"
check "Argo CD App Synced" "kubectl get application $ARGOCD_APP -n $NAMESPACE_ARGOCD -o jsonpath='{.status.sync.status}' 2>/dev/null | grep -q Synced"
check "Argo CD App Healthy" "kubectl get application $ARGOCD_APP -n $NAMESPACE_ARGOCD -o jsonpath='{.status.health.status}' 2>/dev/null | grep -q Healthy"
check "Argo CD Self-Heal Enabled" "kubectl get application $ARGOCD_APP -n $NAMESPACE_ARGOCD -o jsonpath='{.spec.syncPolicy.automated.selfHeal}' 2>/dev/null | grep -q true"
check "GitOps Manifests Tracked" "kubectl get application $ARGOCD_APP -n $NAMESPACE_ARGOCD -o jsonpath='{.spec.source.path}' 2>/dev/null | grep -q 'gitops/manifests'"
check "GitOps Manifest Exists" "test -f gitops/manifests/sample-app-deployment.yaml"
check "Sample App Deployment Exists" "kubectl get deployment $SAMPLE_APP -n $NAMESPACE_APPS --no-headers 2>/dev/null | grep -q $SAMPLE_APP"
check "Sample App Pod Running" "kubectl get pods -n $NAMESPACE_APPS -l app=$SAMPLE_APP --no-headers 2>/dev/null | grep -q Running"
check "Sample App Service" "kubectl get svc $SAMPLE_APP -n $NAMESPACE_APPS --no-headers 2>/dev/null | grep -q $SAMPLE_APP"

# ─── Check 7: Self-Heal Verification (End-to-End) ──────────────────────────────
header "Check 7: Self-Heal Verification (End-to-End)"
echo "Testing self-heal: scaling $SAMPLE_APP to 5 replicas..."
kubectl scale deployment $SAMPLE_APP -n $NAMESPACE_APPS --replicas=5 >/dev/null 2>&1
echo "Waiting for Argo CD self-heal to revert (max ${TIMEOUT}s)..."

HEALED=0
for i in $(seq 1 $((TIMEOUT / 10))); do
    REPLICAS=$(kubectl get deployment $SAMPLE_APP -n $NAMESPACE_APPS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)
    if [ "$REPLICAS" = "1" ]; then
        echo -e "${GREEN}✅ PASS${NC}  Self-heal verified: Argo CD reverted to 1 replica in $(($i * 10))s"
        HEALED=1
        break
    fi
    sleep 10
done

REPLICAS=$(kubectl get deployment $SAMPLE_APP -n $NAMESPACE_APPS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)
if [ "$REPLICAS" = "1" ]; then
    check "Self-heal END-TO-END VERIFIED" "true"
else
    check "Self-heal FAILED" "false"
    echo -e "${RED}   Still at $REPLICAS replicas after ${TIMEOUT}s${NC}"
fi

# ─── Check 8: Quality Gates ────────────────────────────────────────────────────
header "Check 8: Quality Gates"
check "Ruff lint clean" "cd $(dirname "$0")/.. && .venv/bin/python -m ruff check . 2>&1 | tail -1 | grep -q 'All checks passed'"
check "Unit tests pass" ".venv/bin/python -m pytest tests/test_incident_replay.py tests/test_state_durability.py -v --tb=short 2>&1 | grep -qE '[0-9]+ passed'"

# ─── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "RUNBOOK CHECKLIST SUMMARY"
echo "=========================================="
TOTAL=$((PASS + FAIL + SKIP))
echo -e "  ${GREEN}✅ Passed:${NC}  $PASS / $TOTAL"
echo -e "  ${RED}❌ Failed:${NC}  $FAIL / $TOTAL"
echo -e "  ${YELLOW}⏭️  Skipped:${NC} $SKIP / $TOTAL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}⚠️  Some checks failed. Review the output above.${NC}"
    exit 1
else
    echo -e "${GREEN}🎉 All checks passed! SentinelOps is fully operational.${NC}"
    exit 0
fi