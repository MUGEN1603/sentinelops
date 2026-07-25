"""
tests/test_selfheal.py

End-to-end test validating Argo CD's selfHeal behavior:
- Manually scales the sample-app Deployment to an incorrect replica count.
- Waits for Argo CD to detect and revert the drift.
- Asserts the replica count returns to the Git-defined value.

Prerequisites:
  - kind cluster running (`kind get clusters` shows sentinelops)
  - Argo CD deployed and sentinelops-app Application in Synced state
  - KUBECONFIG pointing to the sentinelops kind cluster

Run: pytest tests/test_selfheal.py -v -s --timeout=300
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# This test requires the live k8s cluster
kubernetes = pytest.importorskip("kubernetes", reason="kubernetes Python client required")


# ── Constants ─────────────────────────────────────────────────────────────────

NAMESPACE          = "apps"
DEPLOYMENT_NAME    = "sample-app"
GIT_REPLICA_COUNT  = 1          # Must match gitops/manifests/sample-app-deployment.yaml
DRIFT_REPLICA_COUNT = 5         # The "wrong" value we'll set manually
SELFHEAL_TIMEOUT_S = 300        # Argo CD's default sync interval is 3 minutes


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _k8s_available() -> bool:
    """Return True if we can reach a Kubernetes cluster (in-cluster OR kubeconfig)."""
    from kubernetes import config
    try:
        config.load_incluster_config()
        return True
    except config.config_exception.ConfigException:
        try:
            config.load_kube_config()
            return True
        except Exception:
            return False
    except Exception:
        return False


# Skip the entire module if no Kubernetes cluster is reachable.
pytestmark = pytest.mark.skipif(
    not _k8s_available(),
    reason="No Kubernetes cluster reachable — run `kind create cluster` first"
)


@pytest.fixture(scope="module")
def k8s_client():
    """Load in-cluster or local kubeconfig and return the AppsV1Api client."""
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.config_exception.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_replicas(apps_v1, namespace: str, name: str) -> int:
    """Return the current .spec.replicas for a Deployment."""
    dep = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
    return dep.spec.replicas


def scale_deployment(apps_v1, namespace: str, name: str, replicas: int) -> None:
    """Scale a Deployment to the specified replica count via the k8s API."""
    from kubernetes.client.models import V1Scale, V1ScaleSpec, V1ObjectMeta
    scale = V1Scale(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1ScaleSpec(replicas=replicas)
    )
    apps_v1.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=scale)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestArgoCDSelfHeal:
    """
    Validates the Argo CD selfHeal property on the sentinelops-app Application.

    These tests require the live kind cluster and ARE integration tests,
    not unit tests. They will be skipped if KUBECONFIG is not set or
    the cluster is not reachable.
    """

    @pytest.mark.timeout(SELFHEAL_TIMEOUT_S)
    def test_selfheal_reverts_manual_scale(self, k8s_client):
        """
        Scale the deployment to 5 replicas manually.
        Assert Argo CD reverts it back to GIT_REPLICA_COUNT within selfHeal timeout.
        """
        # ── Precondition: deployment is at expected replica count ─────────────
        current = get_replicas(k8s_client, NAMESPACE, DEPLOYMENT_NAME)
        assert current == GIT_REPLICA_COUNT, (
            f"Deployment not in expected state before test: "
            f"expected {GIT_REPLICA_COUNT} replicas, got {current}. "
            "Ensure Argo CD has synced the cluster before running this test."
        )

        # ── Introduce drift ───────────────────────────────────────────────────
        print(f"\n[selfheal] Scaling {DEPLOYMENT_NAME} to {DRIFT_REPLICA_COUNT} (drift injection)")
        scale_deployment(k8s_client, NAMESPACE, DEPLOYMENT_NAME, DRIFT_REPLICA_COUNT)

        # Argo CD selfHeal can revert drift extremely quickly. Treat both of
        # these outcomes as success paths:
        #   1) We observe the deployment at the drifted replica count, then wait
        #      for it to return to the Git-defined count.
        #   2) Argo CD reverts the deployment before our first observation,
        #      which proves selfHeal reacted immediately.
        observe_timeout = 15
        observe_interval = 1
        elapsed_observe = 0
        replicas = get_replicas(k8s_client, NAMESPACE, DEPLOYMENT_NAME)

        if replicas == DRIFT_REPLICA_COUNT:
            print(f"[selfheal] Drift confirmed immediately: {DEPLOYMENT_NAME} is at {DRIFT_REPLICA_COUNT} replicas")
        elif replicas == GIT_REPLICA_COUNT:
            print(f"[selfheal] ✓ Drift was reverted immediately by Argo CD (replicas already back to {GIT_REPLICA_COUNT})")
            return
        else:
            while elapsed_observe < observe_timeout:
                time.sleep(observe_interval)
                elapsed_observe += observe_interval
                replicas = get_replicas(k8s_client, NAMESPACE, DEPLOYMENT_NAME)
                print(f"[selfheal] observe t+{elapsed_observe}s: replicas={replicas}")

                if replicas == DRIFT_REPLICA_COUNT:
                    print(f"[selfheal] Drift confirmed after {elapsed_observe}s: {DEPLOYMENT_NAME} is at {DRIFT_REPLICA_COUNT} replicas")
                    break
                if replicas == GIT_REPLICA_COUNT:
                    print(f"[selfheal] ✓ Drift was reverted within {elapsed_observe}s by Argo CD")
                    return
            else:
                pytest.fail(
                    f"Deployment never showed an expected state after drift injection. "
                    f"Expected {DRIFT_REPLICA_COUNT} (drift) or {GIT_REPLICA_COUNT} (immediate selfHeal), got {replicas}."
                )

        # ── Wait for Argo CD to revert ────────────────────────────────────────
        print(f"[selfheal] Waiting up to {SELFHEAL_TIMEOUT_S}s for Argo CD selfHeal to revert drift...")
        poll_interval = 10
        elapsed = 0
        reverted = False

        while elapsed < SELFHEAL_TIMEOUT_S:
            time.sleep(poll_interval)
            elapsed += poll_interval
            replicas = get_replicas(k8s_client, NAMESPACE, DEPLOYMENT_NAME)
            print(f"[selfheal] t+{elapsed}s: replicas={replicas}")
            if replicas == GIT_REPLICA_COUNT:
                reverted = True
                break

        assert reverted, (
            f"Argo CD selfHeal did NOT revert the deployment within {SELFHEAL_TIMEOUT_S}s. "
            f"Final replica count: {replicas}. "
            "Check: (1) sentinelops-app Application is in Synced state, "
            "(2) selfHeal:true is set in argocd-app.yaml, "
            "(3) Argo CD pods are healthy in the argocd namespace."
        )
        print(f"[selfheal] ✓ Drift reverted by Argo CD in {elapsed}s")

    def test_git_replica_count_is_correct(self, k8s_client):
        """
        Assert the cluster reflects the Git-defined replica count.
        This is a precondition check before any selfHeal tests.
        """
        replicas = get_replicas(k8s_client, NAMESPACE, DEPLOYMENT_NAME)
        assert replicas == GIT_REPLICA_COUNT, (
            f"Expected {GIT_REPLICA_COUNT} replicas (Git definition), "
            f"but cluster has {replicas}. Run: argocd app sync sentinelops-app"
        )

    def test_observability_pods_running(self):
        """Assert that all observability pods are in Running state."""
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        core_v1 = client.CoreV1Api()
        pods = core_v1.list_namespaced_pod(namespace="observability")

        non_running = [
            p.metadata.name
            for p in pods.items
            if p.status.phase not in ("Running", "Succeeded")
        ]
        assert not non_running, (
            f"Observability pods not Running: {non_running}. "
            "Run: kubectl get pods -n observability"
        )
