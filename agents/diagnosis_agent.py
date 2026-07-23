"""
agents/diagnosis_agent.py — LangGraph node: diagnosis_agent

Responsibility:
  Retrieve similar past incidents from Qdrant and use the LLM to generate
  a root cause analysis (RCA) grounded in that historical context.

Input state keys consumed:
  - incident (dict): The Incident object
  - severity_classification (str): From triage_agent

Output state keys written:
  - rca (str): Root cause analysis narrative
  - similar_incidents (List[str]): IDs of similar incidents retrieved from Qdrant
  - rca_confidence (float): Self-reported LLM confidence score
"""

import json
import logging
import os

import ollama

# Import the qdrant_client MODULE (not the function) so tests can monkeypatch
# `memory.qdrant_client.retrieve_similar` and have the change take effect here.
# A module-level `from memory.qdrant_client import retrieve_similar` would bind
# the function name at import time and prevent monkeypatch from affecting us.
from memory import qdrant_client as _qdrant_memory

log = logging.getLogger("diagnosis-agent")

MODEL = os.getenv("OLLAMA_MODEL", "qwen3-coder:latest")

SYSTEM_PROMPT_TEMPLATE = """\
You are a Kubernetes SRE expert performing root cause analysis.

Here are {n} similar past incidents retrieved from memory (ranked by similarity):
{similar_context}

Based on the incident below and the historical patterns above, provide a thorough root cause analysis.

Respond in this exact JSON format (no markdown, no extra text):
{{
  "probable_cause": "<one clear sentence naming the root cause>",
  "detailed_analysis": "<2-5 sentence technical explanation>",
  "evidence": ["<log line or metric that supports this>", "..."],
  "confidence": <float between 0.0 and 1.0>,
  "recommended_next_step": "<what to investigate or do next>"
}}
"""


def diagnosis_agent(state: dict) -> dict:
    """
    LangGraph node function for root cause diagnosis.

    Reads incident + severity_classification from state.
    Queries Qdrant for top-3 similar historical incidents.
    Sends enriched context to LLM for RCA generation.
    Writes rca, similar_incidents, and rca_confidence to state.
    """
    incident = state.get("incident", {})
    severity = state.get("severity_classification", "unknown")

    log.info("Diagnosis started for incident id=%s (classified=%s)",
             incident.get("id"), severity)

    # ── Build Qdrant query text ──────────────────────────────────────────────
    # Combine workload name, alert labels, and first 10 log lines into a single query
    log_excerpt = " | ".join(incident.get("recent_logs", [])[:10])
    query_text = (
        f"{incident.get('workload', '')} "
        f"{json.dumps(incident.get('alert_labels', {}))} "
        f"{log_excerpt}"
    ).strip()

    # Resolve at call-time from the module so pytest monkeypatch can swap it.
    retrieve_similar = _qdrant_memory.retrieve_similar
    similar = retrieve_similar(query_text, k=3)
    similar_ids = [s["id"] for s in similar]

    # ── Format similar incidents for LLM context ──────────────────────────────
    if similar:
        context_lines = [
            f"{i+1}. [score={s['score']:.3f}] RCA: {s['rca']} | Outcome: {s['outcome']}"
            for i, s in enumerate(similar)
        ]
        similar_context = "\n".join(context_lines)
    else:
        similar_context = "No similar past incidents found in memory."

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        n=len(similar),
        similar_context=similar_context
    )

    # ── Sanitise recent_metrics before passing to LLM ─────────────────────────
    # The Incident schema carries metric-fetch failures in `metric_fetch_errors`
    # (a List[str]), separate from `recent_metrics` (Dict[str, float]). For
    # backward-compat with any raw dicts that still embed the legacy
    # "_fetch_errors" key, we also pop it defensively here.
    raw_metrics = dict(incident.get("recent_metrics", {}))
    legacy_errors = raw_metrics.pop("_fetch_errors", None)
    fetch_errors = incident.get("metric_fetch_errors") or legacy_errors or []

    numeric_metrics = {
        k: v for k, v in raw_metrics.items()
        if isinstance(v, (int, float))
    }

    # Append a data-quality warning if some metrics failed to fetch
    if fetch_errors:
        log.warning("Incomplete metrics for incident=%s — failed: %s", incident.get("id"), fetch_errors)
        metrics_note = (
            f"\n\n[CONTEXT NOTE] The following metrics could not be fetched and are missing: "
            f"{fetch_errors}. Factor this uncertainty into your confidence score."
        )
    else:
        metrics_note = ""



    # ── Compact incident for LLM ──────────────────────────────────────────────
    incident_for_llm = {
        "workload":       incident.get("workload"),
        "namespace":      incident.get("namespace"),
        "severity":       severity,
        "alert_labels":   incident.get("alert_labels", {}),
        "recent_logs":    incident.get("recent_logs", [])[:30],
        # Use sanitised numeric_metrics — _fetch_errors key is stripped above
        "recent_metrics": numeric_metrics,
    }

    # Append data-quality note to system prompt if metrics are incomplete
    if metrics_note:
        system_prompt += metrics_note


    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": json.dumps(incident_for_llm, indent=2)}
            ]
        )
        raw = response["message"]["content"].strip()
        parsed = json.loads(raw)

        rca_text = (
            f"Probable cause: {parsed.get('probable_cause', '')}\n\n"
            f"Analysis: {parsed.get('detailed_analysis', '')}\n\n"
            f"Evidence: {', '.join(parsed.get('evidence', []))}\n\n"
            f"Next step: {parsed.get('recommended_next_step', '')}"
        )
        confidence = float(parsed.get("confidence", 0.5))

    except json.JSONDecodeError:
        log.warning("Diagnosis LLM returned non-JSON — using raw text as RCA")
        rca_text   = raw
        confidence = 0.3
    except Exception as exc:
        log.error("Diagnosis LLM call failed: %s", exc)
        rca_text   = f"Diagnosis failed: {exc}"
        confidence = 0.0

    log.info("Diagnosis complete: confidence=%.2f similar_count=%d", confidence, len(similar))

    state["rca"]              = rca_text
    state["similar_incidents"] = similar_ids
    state["rca_confidence"]   = confidence
    return state
