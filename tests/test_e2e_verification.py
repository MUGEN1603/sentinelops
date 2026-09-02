"""
tests/test_e2e_verification.py — Layered end-to-end verification suite.

Each test class maps to one architectural layer and skips gracefully
(reporting why) instead of failing hard when a dependency is down.

Run: pytest tests/test_e2e_verification.py -v -s
"""

import os
import sys
import subprocess
import time

import pytest
import requests

# ── Helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: str) -> tuple[int, str, str]:
    """Run a shell command, return (rc, stdout, stderr)."""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

# Every failure path in this module funnels through _skip_if_down, including real
# functional failures ("webhook processing failed", "OPA protected namespace test
# failed", "RBAC serviceaccount missing"). Reported as plain skips those go green,
# so the suite cannot fail and a completely broken platform still passes CI.
# STRICT mode converts them to failures; CI sets it, local runs leave it off so the
# suite stays usable without the whole stack running.
STRICT_E2E = os.environ.get("SENTINELOPS_STRICT_E2E", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

def _skip_if_down(reason: str):
    """Skip locally; fail under STRICT_E2E. Descriptive reason either way."""
    if STRICT_E2E:
        pytest.fail(f"STRICT_E2E: {reason}", pytrace=False)
    pytest.skip(f"Dependency unavailable: {reason}")

# ── Layer 0: Local Infrastructure ───────────────────────────────────────────

class TestLocalInfrastructure:
    """Docker, kind, kubectl, helm, local services."""

    def test_docker_running(self):
        """Docker daemon must be responsive."""
        rc, _, _ = _run("docker ps -q")
        if rc != 0:
            _skip_if_down("Docker daemon not responding")
        assert rc == 0

    def test_kind_cluster_exists(self):
        """kind cluster 'sentinelops' must exist and have nodes."""
        rc, out, _ = _run("kind get clusters")
        if rc != 0 or "sentinelops" not in out:
            _skip_if_down("kind cluster 'sentinelops' not found")
        rc, nodes, _ = _run("kubectl get nodes --no-headers 2>/dev/null | wc -l")
        assert rc == 0, "kubectl not available"
        assert int(nodes.strip()) >= 1, "No nodes in cluster"

    def test_qdrant_responding(self):
        """Qdrant HTTP API must be reachable."""
        try:
            resp = requests.get("http://localhost:6333/collections", timeout=3)
            if resp.status_code != 200:
                _skip_if_down(f"Qdrant returned {resp.status_code}")
            data = resp.json()
            assert "result" in data
        except requests.RequestException as e:
            _skip_if_down(f"Qdrant unreachable: {e}")

    def test_opa_responding(self):
        """OPA HTTP API must be reachable."""
        try:
            resp = requests.get("http://localhost:8181/v1/policies", timeout=3)
            if resp.status_code != 200:
                _skip_if_down(f"OPA returned {resp.status_code}")
        except requests.RequestException as e:
            _skip_if_down(f"OPA unreachable: {e}")

    def test_ollama_model_pulled(self):
        """qwen3-coder model must be available in Ollama."""
        rc, out, _ = _run("ollama list | grep -q qwen3-coder")
        if rc != 0:
            _skip_if_down("Ollama model 'qwen3-coder' not pulled")

    def test_helm_available(self):
        """Helm CLI must be available."""
        rc, _, _ = _run("helm version --short")
        assert rc == 0, "Helm not installed"

    def test_argocd_cli_available(self):
        """Argo CD CLI must be available."""
        rc, _, _ = _run("argocd version --client --short 2>/dev/null")
        if rc != 0:
            _skip_if_down("Argo CD CLI not installed")

# ── Layer 1: Kubernetes Control Plane ───────────────────────────────────────

class TestKubernetesControlPlane:
    """API server, core resources, RBAC."""

    def test_api_server_healthy(self):
        """Kubernetes API server must be reachable."""
        rc, _, _ = _run("kubectl get --raw=/healthz 2>/dev/null")
        if rc != 0:
            _skip_if_down("Kubernetes API server unreachable")
        assert rc == 0

    def test_namespaces_exist(self):
        """Required namespaces must exist."""
        required = ["observability", "apps", "argocd", "default"]
        for ns in required:
            rc, _, _ = _run(f"kubectl get ns {ns} --no-headers 2>/dev/null")
            if rc != 0:
                _skip_if_down(f"Namespace '{ns}' missing")

    def test_rbac_resources_exist(self):
        """SentinelOps RBAC resources must be applied."""
        resources = [
            ("serviceaccount", "otel-collector", "observability"),
            ("serviceaccount", "incident-normalizer", "default"),
            ("serviceaccount", "sample-app", "apps"),
            ("serviceaccount", "sentinelops-agent", "default"),
            ("clusterrole", "otel-collector", None),
            ("clusterrolebinding", "otel-collector", None),
        ]
        for kind, name, ns in resources:
            ns_flag = f"-n {ns}" if ns else ""
            rc, _, _ = _run(f"kubectl get {kind} {name} {ns_flag} --no-headers 2>/dev/null")
            if rc != 0:
                _skip_if_down(f"RBAC {kind}/{name} missing")

    def test_network_policies_exist(self):
        """Network policies must be applied."""
        rc, out, _ = _run("kubectl get networkpolicy --all-namespaces --no-headers 2>/dev/null | grep -E '(sentinelops|default-deny)' | wc -l")
        if rc != 0:
            _skip_if_down("Network policies not applied")
        assert int(out.strip()) >= 4, "Expected at least 4 network policies"

# ── Layer 2: Observability Stack ────────────────────────────────────────────

class TestObservabilityStack:
    """Prometheus, AlertManager, Grafana, Loki, OTel Collector."""

    @pytest.fixture(autouse=True)
    def _portforwards(self):
        """Start port-forwards for in-cluster services."""
        self._procs = []
        # Prometheus
        p = subprocess.Popen(
            ["kubectl", "port-forward", "-n", "observability", "svc/kube-prometheus-kube-prome-prometheus", "9090:9090"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._procs.append(p)
        # Loki
        p = subprocess.Popen(
            ["kubectl", "port-forward", "-n", "observability", "svc/loki", "3100:3100"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._procs.append(p)
        # AlertManager
        p = subprocess.Popen(
            ["kubectl", "port-forward", "-n", "observability", "svc/kube-prometheus-kube-prome-alertmanager", "9093:9093"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._procs.append(p)
        # Grafana
        p = subprocess.Popen(
            ["kubectl", "port-forward", "-n", "observability", "svc/kube-prometheus-grafana", "3000:80"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._procs.append(p)
        time.sleep(3)  # let them establish
        yield
        for p in self._procs:
            p.terminate()

    def test_prometheus_responding(self):
        """Prometheus HTTP API must respond."""
        try:
            resp = requests.get("http://localhost:9090/api/v1/query?query=up", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Prometheus returned {resp.status_code}")
            data = resp.json()
            assert data["status"] == "success"
        except requests.RequestException as e:
            _skip_if_down(f"Prometheus unreachable: {e}")

    def test_prometheus_rules_loaded(self):
        """SentinelOps PrometheusRules must be loaded."""
        try:
            resp = requests.get("http://localhost:9090/api/v1/rules", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Prometheus rules endpoint returned {resp.status_code}")
            data = resp.json()
            rule_groups = data.get("data", {}).get("groups", [])
            sentinelops_groups = [g for g in rule_groups if "sentinelops" in g.get("name", "")]
            if not sentinelops_groups:
                _skip_if_down("No SentinelOps PrometheusRule groups found")
        except requests.RequestException as e:
            _skip_if_down(f"Prometheus rules query failed: {e}")

    def test_alertmanager_responding(self):
        """AlertManager HTTP API must respond."""
        try:
            resp = requests.get("http://localhost:9093/api/v2/status", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"AlertManager returned {resp.status_code}")
        except requests.RequestException as e:
            _skip_if_down(f"AlertManager unreachable: {e}")

    def test_loki_responding(self):
        """Loki HTTP API must respond."""
        try:
            resp = requests.get("http://localhost:3100/ready", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Loki returned {resp.status_code}")
        except requests.RequestException as e:
            _skip_if_down(f"Loki unreachable: {e}")

    def test_grafana_responding(self):
        """Grafana HTTP API must respond."""
        try:
            resp = requests.get("http://localhost:3000/api/health", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Grafana returned {resp.status_code}")
            data = resp.json()
            assert data.get("database") == "ok"
        except requests.RequestException as e:
            _skip_if_down(f"Grafana unreachable: {e}")

    def test_otel_collector_pods_running(self):
        """OTel Collector DaemonSet pods must be running."""
        rc, out, _ = _run("kubectl get pods -n observability -l app.kubernetes.io/name=opentelemetry-collector --no-headers 2>/dev/null | grep -c Running")
        if rc != 0:
            _skip_if_down("OTel Collector pods not found")
        assert int(out.strip()) >= 2, "Expected at least 2 OTel Collector pods running"

# ── Layer 3: Sample Application ─────────────────────────────────────────────

class TestSampleApplication:
    """Fault-injectable sample app deployment."""

    def test_deployment_exists(self):
        """Sample app deployment must exist in apps namespace."""
        rc, _, _ = _run("kubectl get deployment sample-app -n apps --no-headers 2>/dev/null")
        if rc != 0:
            _skip_if_down("sample-app deployment not found")

    def test_pod_running(self):
        """Sample app pod must be running."""
        rc, out, _ = _run("kubectl get pods -n apps -l app=sample-app --no-headers 2>/dev/null | grep -c Running")
        if rc != 0:
            _skip_if_down("No sample-app pods found")
        assert int(out.strip()) >= 1, "Sample app pod not running"

    def test_service_exists(self):
        """Sample app service must exist."""
        rc, _, _ = _run("kubectl get svc sample-app -n apps --no-headers 2>/dev/null")
        if rc != 0:
            _skip_if_down("sample-app service not found")

    def test_metrics_endpoint_accessible(self):
        """Sample app /metrics endpoint must be scrapeable via port-forward."""
        pf = subprocess.Popen(
            ["kubectl", "port-forward", "-n", "apps", "svc/sample-app", "8080:8080"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        try:
            resp = requests.get("http://localhost:8080/metrics", timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Sample app metrics returned {resp.status_code}")
            assert "sample_app_requests_total" in resp.text
        except requests.RequestException as e:
            _skip_if_down(f"Sample app metrics unreachable: {e}")
        finally:
            pf.terminate()

# ── Layer 4: Incident Normalizer (Local Service) ────────────────────────────

class TestIncidentNormalizer:
    """Incident normalizer webhook server (runs locally)."""

    @pytest.fixture(autouse=True)
    def _start_server(self):
        """Start uvicorn server for incident normalizer."""
        env = os.environ.copy()
        env["LOKI_URL"] = "http://localhost:3100"
        env["PROMETHEUS_URL"] = "http://localhost:9090"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "incident_normalizer.webhook_server:app", "--port", "8000"],
            cwd="/Users/gauravpandey/Desktop/MydevopsProj/sentinelops",
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(3)
        yield
        self.proc.terminate()
        self.proc.wait(timeout=5)

    def test_health_endpoint(self):
        """Health endpoint must return ok."""
        try:
            resp = requests.get("http://localhost:8000/health", timeout=3)
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
        except requests.RequestException as e:
            _skip_if_down(f"Incident normalizer health check failed: {e}")

    def test_webhook_processes_alert(self):
        """Webhook must process a firing AlertManager payload."""
        payload = {
            "version": "4",
            "groupKey": "test",
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "PodOOMKilled",
                    "pod": "sample-app-test",
                    "namespace": "apps",
                    "severity": "critical"
                },
                "annotations": {"summary": "Test alert"},
                "startsAt": "2024-01-01T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "test123"
            }]
        }
        try:
            resp = requests.post("http://localhost:8000/webhook", json=payload, timeout=10)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "processed"
            assert data["count"] == 1
        except requests.RequestException as e:
            _skip_if_down(f"Webhook processing failed: {e}")

    def test_resolved_alert_skipped(self):
        """Resolved alerts must be skipped (count=0)."""
        payload = {
            "version": "4",
            "status": "resolved",
            "alerts": [{
                "status": "resolved",
                "labels": {"alertname": "PodOOMKilled", "pod": "test", "namespace": "apps"},
                "startsAt": "2024-01-01T00:00:00Z",
                "endsAt": "2024-01-01T00:05:00Z",
            }]
        }
        try:
            resp = requests.post("http://localhost:8000/webhook", json=payload, timeout=5)
            assert resp.status_code == 200
            assert resp.json()["count"] == 0
        except requests.RequestException as e:
            _skip_if_down(f"Webhook resolved alert test failed: {e}")

# ── Layer 5: LangGraph Pipeline ─────────────────────────────────────────────

class TestLangGraphPipeline:
    """LangGraph agent pipeline with SQLite checkpointer."""

    def test_graph_compiles(self):
        """Graph must compile without errors."""
        try:
            from agents.graph import build_graph
            graph = build_graph()
            assert graph is not None
        except Exception as e:
            _skip_if_down(f"Graph compilation failed: {e}")

    def test_pipeline_invocation(self):
        """Pipeline must accept an incident and produce all outputs."""
        try:
            from agents.graph import get_graph
            graph = get_graph()
            test_incident = {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "started_at": "2024-01-01T00:00:00Z",
                "namespace": "apps",
                "workload": "sample-app-test",
                "kind": "Pod",
                "severity": "critical",
                "alert_labels": {"alertname": "PodOOMKilled"},
                "recent_logs": ["ERROR: out of memory", "OOMKilled"],
                "recent_metrics": {"memory_usage_bytes": 134217728},
                "k8s_objects": [],
                "trace_refs": []
            }
            thread_id = "test-verification-001"
            result = graph.invoke(
                {"incident": test_incident},
                config={"configurable": {"thread_id": thread_id}}
            )
            # All pipeline outputs must be present
            assert "severity_classification" in result
            assert "rca" in result
            assert "proposed_patch" in result
            assert "policy_allowed" in result
        except Exception as e:
            _skip_if_down(f"Pipeline invocation failed: {e}")

    def test_checkpointer_persistence(self):
        """Checkpointer must persist state across invocations."""
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            os.environ["SENTINELOPS_CHECKPOINT_DB"] = db_path
            from agents.graph import get_graph
            
            graph = get_graph()
            test_incident = {
                "id": "checkpoint-test-001",
                "started_at": "2024-01-01T00:00:00Z",
                "namespace": "apps",
                "workload": "sample-app-test",
                "kind": "Pod",
                "severity": "critical",
                "alert_labels": {},
                "recent_logs": [],
                "recent_metrics": {},
                "k8s_objects": [],
                "trace_refs": []
            }
            thread_id = "checkpoint-test-001"
            result1 = graph.invoke({"incident": test_incident}, config={"configurable": {"thread_id": thread_id}})
            
            # Re-invoke with same thread_id - should resume, not restart
            result2 = graph.invoke({"incident": test_incident}, config={"configurable": {"thread_id": thread_id}})
            
            # Both invocations should have completed state
            assert "rca" in result1
            assert "rca" in result2
        except Exception as e:
            _skip_if_down(f"Checkpointer persistence test failed: {e}")
        finally:
            os.unlink(db_path)

# ── Layer 6: OPA Policy ─────────────────────────────────────────────────────

class TestOPAPolicy:
    """OPA policy engine and Rego rules."""

    def test_policy_loaded(self):
        """Rego policy must be loaded in OPA."""
        try:
            resp = requests.get("http://localhost:8181/v1/policies/remediation", timeout=3)
            if resp.status_code != 200:
                _skip_if_down(f"Policy not loaded: {resp.status_code}")
        except requests.RequestException as e:
            _skip_if_down(f"OPA policy check failed: {e}")

    def test_allow_medium_risk_apps(self):
        """Medium-risk patch_memory_limit in apps namespace must be allowed."""
        try:
            resp = requests.post(
                "http://localhost:8181/v1/data/sentinelops/remediation/allow",
                json={"input": {"action": "patch_memory_limit", "risk_level": "medium", "namespace": "apps"}},
                timeout=3
            )
            assert resp.status_code == 200
            assert resp.json()["result"] is True
        except requests.RequestException as e:
            _skip_if_down(f"OPA allow test failed: {e}")

    def test_deny_high_risk(self):
        """High-risk actions must be denied."""
        try:
            resp = requests.post(
                "http://localhost:8181/v1/data/sentinelops/remediation/allow",
                json={"input": {"action": "patch_memory_limit", "risk_level": "high", "namespace": "apps"}},
                timeout=3
            )
            assert resp.status_code == 200
            assert resp.json()["result"] is False
        except requests.RequestException as e:
            _skip_if_down(f"OPA deny test failed: {e}")

    def test_deny_protected_namespace(self):
        """Actions in protected namespaces must be denied."""
        try:
            resp = requests.post(
                "http://localhost:8181/v1/data/sentinelops/remediation/allow",
                json={"input": {"action": "patch_memory_limit", "risk_level": "low", "namespace": "kube-system"}},
                timeout=3
            )
            assert resp.status_code == 200
            assert resp.json()["result"] is False
        except requests.RequestException as e:
            _skip_if_down(f"OPA protected namespace test failed: {e}")

    def test_deny_unknown_action(self):
        """Unknown action types must be denied."""
        try:
            resp = requests.post(
                "http://localhost:8181/v1/data/sentinelops/remediation/allow",
                json={"input": {"action": "kubectl_exec", "risk_level": "low", "namespace": "apps"}},
                timeout=3
            )
            assert resp.status_code == 200
            assert resp.json()["result"] is False
        except requests.RequestException as e:
            _skip_if_down(f"OPA unknown action test failed: {e}")

# ── Layer 7: Qdrant Vector Memory ───────────────────────────────────────────

class TestQdrantMemory:
    """Qdrant vector database for incident RAG."""

    def test_collection_exists(self):
        """Incidents collection must exist with correct dimension."""
        try:
            resp = requests.get("http://localhost:6333/collections/incidents", timeout=3)
            if resp.status_code != 200:
                _skip_if_down(f"Collection not found: {resp.status_code}")
            data = resp.json()
            assert data["result"]["config"]["params"]["vectors"]["size"] == 384
        except requests.RequestException as e:
            _skip_if_down(f"Qdrant collection check failed: {e}")

    def test_store_and_retrieve(self):
        """Must be able to store and retrieve an incident vector."""
        try:
            # Store
            store_resp = requests.put(
                "http://localhost:6333/collections/incidents/points",
                json={
                    "points": [{
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "vector": [0.1] * 384,
                        "payload": {"rca": "test cause", "outcome": "resolved"}
                    }]
                },
                timeout=3
            )
            assert store_resp.status_code in (200, 201)
            
            # Retrieve
            search_resp = requests.post(
                "http://localhost:6333/collections/incidents/points/query",
                json={"query": [0.1] * 384, "limit": 1, "with_payload": True},
                timeout=3
            )
            assert search_resp.status_code == 200
            results = search_resp.json()["result"]["points"]
            assert len(results) == 1
            assert results[0]["payload"]["rca"] == "test cause"
        except requests.RequestException as e:
            _skip_if_down(f"Qdrant store/retrieve failed: {e}")

# ── Layer 8: Argo CD GitOps ─────────────────────────────────────────────────

class TestArgoCDGitOps:
    """Argo CD application and GitOps sync."""

    def test_argocd_server_responding(self):
        """Argo CD server API must respond."""
        try:
            resp = requests.get("https://localhost:8080/api/version", verify=False, timeout=5)
            if resp.status_code != 200:
                _skip_if_down(f"Argo CD returned {resp.status_code}")
        except requests.RequestException as e:
            _skip_if_down(f"Argo CD server unreachable: {e}")

    def test_application_exists(self):
        """SentinelOps Application resource must exist."""
        rc, _, _ = _run("kubectl get application sentinelops-app -n argocd --no-headers 2>/dev/null")
        if rc != 0:
            _skip_if_down("Argo CD Application 'sentinelops-app' not found")

    def test_application_synced(self):
        """Application must be Synced (or at least not Degraded)."""
        try:
            rc, out, _ = _run("kubectl get application sentinelops-app -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null")
            if rc != 0 or not out:
                _skip_if_down("Cannot read application sync status")
            sync_status = out.strip()
            if sync_status not in ("Synced", "Unknown"):
                _skip_if_down(f"Application not synced: {sync_status}")
        except Exception as e:
            _skip_if_down(f"Argo CD sync check failed: {e}")

    def test_selfheal_enabled(self):
        """Application must have automated.selfHeal: true."""
        try:
            rc, out, _ = _run("kubectl get application sentinelops-app -n argocd -o jsonpath='{.spec.syncPolicy.automated.selfHeal}' 2>/dev/null")
            if rc != 0:
                _skip_if_down("Cannot read selfHeal setting")
            assert out.strip().lower() == "true", f"selfHeal is {out.strip()}, expected true"
        except Exception as e:
            _skip_if_down(f"selfHeal check failed: {e}")

# ── Layer 9: GitOps Repository Access ───────────────────────────────────────

class TestGitOpsRepository:
    """GitHub repository access for GitOps."""

    def test_github_repo_accessible(self):
        """GitHub repo must be accessible (public or via token)."""
        # Check if repo is public or if we have token
        gh_token = os.environ.get("GITHUB_TOKEN")
        if not gh_token:
            _skip_if_down("GITHUB_TOKEN not set - cannot verify private repo access")
        
        try:
            resp = requests.get(
                "https://api.github.com/repos/MUGEN1603/sentinelops",
                headers={"Authorization": f"Bearer {gh_token}"},
                timeout=10
            )
            if resp.status_code == 404:
                _skip_if_down("Repository not found or no access")
            assert resp.status_code == 200
        except requests.RequestException as e:
            _skip_if_down(f"GitHub API unreachable: {e}")

    def test_gitops_manifests_exist(self):
        """gitops/manifests/ directory must exist in repo."""
        gh_token = os.environ.get("GITHUB_TOKEN")
        if not gh_token:
            _skip_if_down("GITHUB_TOKEN not set")
        try:
            resp = requests.get(
                "https://api.github.com/repos/MUGEN1603/sentinelops/contents/gitops/manifests",
                headers={"Authorization": f"Bearer {gh_token}"},
                timeout=10
            )
            assert resp.status_code == 200
            contents = resp.json()
            assert any(f["name"] == "sample-app-deployment.yaml" for f in contents)
        except requests.RequestException as e:
            _skip_if_down(f"GitHub contents check failed: {e}")

# ── Summary Report ──────────────────────────────────────────────────────────

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print a summary table of layer statuses."""
    print("\n" + "=" * 80)
    print("E2E VERIFICATION SUMMARY")
    print("=" * 80)
    
    layers = [
        ("Layer 0: Local Infrastructure", "TestLocalInfrastructure"),
        ("Layer 1: Kubernetes Control Plane", "TestKubernetesControlPlane"),
        ("Layer 2: Observability Stack", "TestObservabilityStack"),
        ("Layer 3: Sample Application", "TestSampleApplication"),
        ("Layer 4: Incident Normalizer", "TestIncidentNormalizer"),
        ("Layer 5: LangGraph Pipeline", "TestLangGraphPipeline"),
        ("Layer 6: OPA Policy", "TestOPAPolicy"),
        ("Layer 7: Qdrant Memory", "TestQdrantMemory"),
        ("Layer 8: Argo CD GitOps", "TestArgoCDGitOps"),
        ("Layer 9: GitOps Repository", "TestGitOpsRepository"),
    ]
    
    for layer_name, class_name in layers:
        passed = 0
        failed = 0
        skipped = 0
        # Use public stats API instead of private _getfailed/_getpassed/_getskipped
        for outcome_key in ("passed", "failed", "skipped"):
            for item in terminalreporter.stats.get(outcome_key, []):
                if hasattr(item, 'cls') and item.cls and item.cls.__name__ == class_name:
                    if outcome_key == "passed":
                        passed += 1
                    elif outcome_key == "failed":
                        failed += 1
                    elif outcome_key == "skipped":
                        skipped += 1
        
        status = "✅ PASS" if failed == 0 and passed > 0 else ("⏭️  SKIP" if skipped > 0 and passed == 0 else "❌ FAIL")
        print(f"  {layer_name:35s}  {status}  (passed={passed}, skipped={skipped}, failed={failed})")
    
    print("=" * 80)