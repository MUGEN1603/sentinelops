"""
incident-normalizer/webhook_server.py

Receives AlertManager webhook POSTs, enriches them with Loki logs,
builds a typed Incident object, and dispatches to the LangGraph pipeline.

Run:
    uvicorn incident-normalizer.webhook_server:app --port 8000 --reload

Exit check:
    curl -X POST localhost:8000/webhook \
      -H "Content-Type: application/json" \
      -d '{"alerts":[{"labels":{"pod":"sample-app-xyz","namespace":"apps","severity":"critical"}}]}'
    # expect: {"status":"processed","count":1}
"""

from __future__ import annotations

import datetime
import os
import time
import uuid
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# sys.path manipulation needed when running from repo root
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.contracts import Incident

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("incident-normalizer")

app = FastAPI(
    title="SentinelOps Incident Normalizer",
    description="Receives AlertManager webhooks, enriches with Loki logs, dispatches to LangGraph pipeline.",
    version="1.0.0"
)

# ── Config (override via environment variables) ───────────────────────────────
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
LOKI_LOG_MINUTES = int(os.getenv("LOKI_LOG_MINUTES", "5"))
LOKI_LOG_LIMIT = int(os.getenv("LOKI_LOG_LIMIT", "100"))

# Bounded worker pool for pipeline dispatch — prevents unbounded thread spawn
# under alert bursts. Ollama is single-concurrency; default of 2 lets one work
# while another queues without overwhelming the local LLM runtime.
PIPELINE_WORKERS = int(os.getenv("PIPELINE_WORKERS", "2"))
_pipeline_pool = ThreadPoolExecutor(
    max_workers=PIPELINE_WORKERS,
    thread_name_prefix="sentinelops-pipe"
)

# ─────────────────────────────────────────────────────────────────────────────
# Loki log fetcher
# ─────────────────────────────────────────────────────────────────────────────

def fetch_loki_logs(pod_name: str, namespace: str = "apps") -> list[str]:
    """
    Query Loki for recent log lines from a specific pod.

    Uses LogQL stream selector: {namespace="<ns>", pod="<pod>"}
    Returns a flat list of log line strings, newest-last.
    Falls back to an empty list with a diagnostic entry on any error.
    """
    if not pod_name or pod_name == "unknown":
        return ["[normalizer] pod_name unknown — log fetch skipped"]

    end_ns = int(time.time() * 1e9)          # nanoseconds
    start_ns = int((time.time() - LOKI_LOG_MINUTES * 60) * 1e9)

    params = {
        "query": f'{{namespace="{namespace}", pod="{pod_name}"}}',
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(LOKI_LOG_LIMIT),
        "direction": "forward",
    }

    try:
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params=params,
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        lines: list[str] = []
        for stream in data.get("data", {}).get("result", []):
            for _, line in stream.get("values", []):
                lines.append(line)
        log.info("Fetched %d log lines for pod=%s", len(lines), pod_name)
        return lines
    except requests.exceptions.ConnectionError:
        log.warning("Loki unreachable at %s — returning empty logs", LOKI_URL)
        return [f"[normalizer] Loki unreachable at {LOKI_URL}"]
    except Exception as exc:
        log.error("Loki fetch error for pod=%s: %s", pod_name, exc)
        return [f"[normalizer] log fetch error: {exc}"]


# ─────────────────────────────────────────────────────────────────────────────
# Metrics fetcher (stub — extend to query Prometheus HTTP API)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_recent_metrics(pod_name: str, namespace: str) -> dict[str, float]:
    """
    Query Prometheus for key point-in-time metrics for the affected pod.

    Queries three PromQL expressions:
      - container_memory_usage_bytes
      - rate(container_cpu_usage_seconds_total[5m])
      - kube_pod_container_status_restarts_total

    Returns a dict of {metric_name: float_value}.
    If Prometheus is unreachable or a query returns no data, the metric is
    absent from the dict and its name is logged + recorded in _fetch_errors
    so downstream agents know the context is incomplete (not silently missing).

    The special key "_fetch_errors" holds a list of metric names that failed.
    Downstream agents should treat its presence as a signal that RCA may have
    incomplete metric context.
    """
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    log.info("Fetching metrics for pod=%s ns=%s from %s", pod_name, namespace, PROMETHEUS_URL)

    queries = {
        "memory_usage_bytes": (
            f'container_memory_usage_bytes{{namespace="{namespace}", pod="{pod_name}"}}'
        ),
        "cpu_usage_cores": (
            f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod="{pod_name}"}}[5m])'
        ),
        "restart_count": (
            f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod="{pod_name}"}}'
        ),
    }

    metrics: dict[str, float] = {}
    failed_metrics: list[str] = []

    for metric_name, promql in queries.items():
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": promql},
                timeout=3
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                metrics[metric_name] = float(results[0]["value"][1])
                log.debug("Metric %s = %s for pod=%s", metric_name, metrics[metric_name], pod_name)
            else:
                # Query succeeded but returned no series — pod may not be in Prometheus yet
                log.warning(
                    "Prometheus returned empty result for metric=%s pod=%s (pod not yet scraped?)",
                    metric_name, pod_name
                )
                failed_metrics.append(f"{metric_name}:no_data")
        except requests.exceptions.ConnectionError:
            log.warning(
                "Prometheus unreachable at %s — metric=%s will be missing from incident context",
                PROMETHEUS_URL, metric_name
            )
            failed_metrics.append(f"{metric_name}:prometheus_unreachable")
        except requests.exceptions.Timeout:
            log.warning("Prometheus query timed out for metric=%s pod=%s", metric_name, pod_name)
            failed_metrics.append(f"{metric_name}:timeout")
        except Exception as exc:
            log.warning(
                "Prometheus query failed for metric=%s pod=%s: %s",
                metric_name, pod_name, exc
            )
            failed_metrics.append(f"{metric_name}:{type(exc).__name__}")

    if failed_metrics:
        log.warning(
            "Incomplete metrics for pod=%s: %d/%d metrics failed: %s",
            pod_name, len(failed_metrics), len(queries), failed_metrics
        )
        # Surface missing-metric context as a separate field rather than
        # polluting recent_metrics (which is typed Dict[str, float] in the
        # Pydantic Incident schema and would raise ValidationError on a list).
        metrics["metric_fetch_errors"] = failed_metrics  # type: ignore[assignment]

    log.info(
        "Metrics fetch complete for pod=%s: %d/%d succeeded",
        pod_name, len(queries) - len(failed_metrics), len(queries)
    )
    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch to LangGraph pipeline
# ─────────────────────────────────────────────────────────────────────────────

def dispatch_to_pipeline(incident: Incident) -> None:
    """
    Hand the incident off to the LangGraph agent pipeline.
    Import is deferred to avoid circular imports at module load time.
    """
    try:
        from agents.graph import get_graph
        graph = get_graph()
        config = {"configurable": {"thread_id": incident.id}}
        result = graph.invoke({"incident": incident.model_dump()}, config=config)
        log.info(
            "Pipeline completed for incident=%s | policy_allowed=%s | pr_url=%s",
            incident.id,
            result.get("policy_allowed"),
            result.get("gitops_change", {}).get("pr_url", "N/A")
        )
    except Exception as exc:
        log.error("Pipeline dispatch failed for incident=%s: %s", incident.id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_alert(request: Request) -> JSONResponse:
    """
    AlertManager webhook receiver.

    AlertManager POST body shape:
    {
      "version": "4",
      "groupKey": "...",
      "status": "firing",
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "PodCrashLooping", "pod": "...", "namespace": "apps", "severity": "critical"},
          "annotations": {"summary": "..."},
          "startsAt": "2024-01-01T00:00:00Z",
          "endsAt": "0001-01-01T00:00:00Z",
          "fingerprint": "..."
        }
      ]
    }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    log.info("Received webhook: status=%s alerts=%d",
             payload.get("status"), len(payload.get("alerts", [])))

    incidents_built: list[dict] = []

    for alert in payload.get("alerts", []):
        if alert.get("status") == "resolved":
            log.info("Skipping resolved alert: %s", alert.get("labels", {}).get("alertname"))
            continue

        labels = alert.get("labels", {})
        pod_name = labels.get("pod", "unknown")
        namespace = labels.get("namespace", "apps")

        # Build incident context: metrics dict and any fetch-error list
        raw_metrics = fetch_recent_metrics(pod_name, namespace)
        metric_errors = raw_metrics.pop("metric_fetch_errors", []) if isinstance(
            raw_metrics.get("metric_fetch_errors"), list
        ) else raw_metrics.pop("_fetch_errors", [])

        incident = Incident(
            id=str(uuid.uuid4()),
            started_at=alert.get("startsAt", datetime.datetime.now(datetime.UTC).isoformat()),
            namespace=namespace,
            workload=pod_name,
            kind="Pod",
            severity=labels.get("severity", "unknown"),
            alert_labels=labels,
            recent_logs=fetch_loki_logs(pod_name, namespace),
            recent_metrics={k: v for k, v in raw_metrics.items() if isinstance(v, (int, float))},
            metric_fetch_errors=metric_errors,
            k8s_objects=[],    # extend: query k8s API for pod spec + events
            trace_refs=[]      # extend: correlate from OTel collector
        )

        log.info("Built incident id=%s workload=%s severity=%s logs=%d",
                 incident.id, incident.workload, incident.severity, len(incident.recent_logs))

        # Bounded async dispatch via the worker pool (avoids unbounded thread
        # spawn under alert bursts). The pool blocks once PIPELINE_WORKERS are busy.
        _pipeline_pool.submit(dispatch_to_pipeline, incident)

        incidents_built.append(incident.model_dump())

    return JSONResponse(content={"status": "processed", "count": len(incidents_built)})


@app.get("/health")
def health() -> dict:
    """Health check endpoint for k8s liveness probe."""
    return {"status": "ok", "service": "incident-normalizer"}
