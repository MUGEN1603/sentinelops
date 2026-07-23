"""
tests/test_incident_replay.py

Tests that the Incident Normalizer correctly parses an AlertManager payload
and builds a valid Incident object.

Run: pytest tests/test_incident_replay.py -v
"""

import json
import sys
import os

import pytest
from fastapi.testclient import TestClient

# Ensure repo root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_ALERTMANAGER_PAYLOAD = {
    "version": "4",
    "groupKey": "{}:{alertname='PodCrashLooping'}",
    "status": "firing",
    "receiver": "sentinelops-webhook",
    "groupLabels": {"alertname": "PodCrashLooping"},
    "commonLabels": {"alertname": "PodCrashLooping", "namespace": "apps"},
    "commonAnnotations": {"summary": "Pod sample-app-abc123 is crash looping"},
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "PodCrashLooping",
                "pod":       "sample-app-abc123",
                "namespace": "apps",
                "severity":  "critical",
            },
            "annotations": {
                "summary": "Pod sample-app-abc123 is crash looping"
            },
            "startsAt":   "2024-01-01T00:00:00Z",
            "endsAt":     "0001-01-01T00:00:00Z",
            "fingerprint": "abc123fingerprint"
        }
    ]
}

RESOLVED_PAYLOAD = {
    "version": "4",
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {"alertname": "PodCrashLooping", "pod": "sample-app-abc123", "namespace": "apps", "severity": "critical"},
            "startsAt": "2024-01-01T00:00:00Z",
            "endsAt":   "2024-01-01T00:05:00Z",
        }
    ]
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWebhookParsing:
    """Tests the webhook endpoint's ability to parse AlertManager payloads."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        """Patch external calls so unit tests don't require live services."""
        # Patch Loki log fetch — return deterministic sample logs
        # Target the alias `incident_normalizer` registered by tests/conftest.py
        monkeypatch.setattr(
            "incident_normalizer.webhook_server.fetch_loki_logs",
            lambda pod_name, namespace="apps": [
                "ERROR: out of memory",
                "WARN: memory usage at 95%",
                "INFO: container started"
            ]
        )
        # Patch Prometheus metrics fetch — return sample metrics
        monkeypatch.setattr(
            "incident_normalizer.webhook_server.fetch_recent_metrics",
            lambda pod_name, namespace: {
                "memory_usage_bytes": 125000000.0,
                "restart_count": 3.0,
                "metric_fetch_errors": ["cpu_usage_cores:timeout"],
            }
        )
        # Patch pipeline dispatch — no-op in unit tests
        monkeypatch.setattr(
            "incident_normalizer.webhook_server.dispatch_to_pipeline",
            lambda incident: None
        )
        from incident_normalizer import webhook_server  # noqa
        self.client = TestClient(webhook_server.app)

    def test_firing_alert_returns_processed(self):
        """A firing alert should be processed and return count=1."""
        resp = self.client.post("/webhook", json=SAMPLE_ALERTMANAGER_PAYLOAD)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "processed"
        assert body["count"] == 1

    def test_resolved_alert_is_skipped(self):
        """Resolved alerts should be skipped (count=0)."""
        resp = self.client.post("/webhook", json=RESOLVED_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_invalid_json_returns_400(self):
        """Non-JSON payloads should return 400."""
        resp = self.client.post(
            "/webhook",
            content="this is not json",
            headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400

    def test_health_endpoint(self):
        """Health check endpoint returns ok."""
        resp = self.client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestIncidentSchema:
    """Tests that the Incident Pydantic model validates correctly."""

    def test_valid_incident_construction(self):
        from agents.contracts import Incident
        incident = Incident(
            id="test-uuid-1234",
            started_at="2024-01-01T00:00:00Z",
            namespace="apps",
            workload="sample-app-abc123",
            kind="Pod",
            severity="critical",
            alert_labels={"alertname": "PodCrashLooping"},
            recent_logs=["ERROR: OOMKilled"],
            recent_metrics={"memory_usage_bytes": 125000000.0},
            metric_fetch_errors=["cpu_usage_cores:timeout"],
            k8s_objects=[],
            trace_refs=[]
        )
        assert incident.id == "test-uuid-1234"
        assert incident.severity == "critical"
        assert len(incident.recent_logs) == 1
        assert incident.metric_fetch_errors == ["cpu_usage_cores:timeout"]

    def test_incident_dict_roundtrip(self):
        """Incident must serialize to dict and reconstruct cleanly."""
        from agents.contracts import Incident
        incident = Incident(
            id="roundtrip-test",
            started_at="2024-01-01T00:00:00Z",
            namespace="apps",
            workload="test-pod",
            kind="Pod",
            severity="warning",
            alert_labels={},
            recent_logs=[],
            recent_metrics={},
            metric_fetch_errors=[],
            k8s_objects=[],
            trace_refs=[]
        )
        serialized = incident.model_dump()
        reconstructed = Incident(**serialized)
        assert reconstructed == incident
