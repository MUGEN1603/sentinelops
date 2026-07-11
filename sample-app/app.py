"""
sample-app/app.py — Fault-injectable FastAPI application.

Endpoints:
  GET /healthy      → liveness probe (always returns ok)
  GET /crash        → immediately kills the process (simulates container crash)
  GET /leak-memory  → allocates 50MB per call (triggers OOMKill with 128Mi limit)
  GET /slow         → sleeps 10s (simulates high latency / timeout alert)
  GET /metrics      → Prometheus-format basic metrics
"""

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
import time
import os

app = FastAPI(title="SentinelOps Sample App", version="1.0.0")

# ── Prometheus metrics ────────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "sample_app_requests_total",
    "Total requests received",
    ["endpoint", "status"]
)
MEMORY_LEAKED_MB = Gauge(
    "sample_app_leaked_memory_mb",
    "Total megabytes leaked so far"
)

# ── In-process memory hog (intentional for fault injection) ───────────────────
memory_hog: list[bytearray] = []


@app.get("/healthy")
def healthy():
    REQUEST_COUNT.labels(endpoint="/healthy", status="ok").inc()
    return {"status": "ok", "timestamp": time.time()}


@app.get("/crash")
def crash():
    """Immediately kill the process — simulates a container crash restart."""
    REQUEST_COUNT.labels(endpoint="/crash", status="crash").inc()
    os._exit(1)  # hard exit; Kubernetes will restart the pod


@app.get("/leak-memory")
def leak_memory():
    """
    Allocate 50MB per call into a process-global list.
    With spec.resources.limits.memory=128Mi, calling this 3× triggers OOMKill.
    """
    memory_hog.append(bytearray(50_000_000))  # 50 MB
    leaked = len(memory_hog) * 50
    MEMORY_LEAKED_MB.set(leaked)
    REQUEST_COUNT.labels(endpoint="/leak-memory", status="ok").inc()
    return {"leaked_mb": leaked, "allocations": len(memory_hog)}


@app.get("/slow")
def slow():
    """Sleep 10 seconds — simulates high-latency service impacting SLOs."""
    REQUEST_COUNT.labels(endpoint="/slow", status="ok").inc()
    time.sleep(10)
    return {"status": "slow response", "slept_seconds": 10}


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
