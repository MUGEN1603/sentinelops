"""
agents/triage_agent.py — LangGraph node: triage_agent

Responsibility:
  Classify the incident severity and urgency using the LLM.
  Outputs a one-word severity classification and a structured triage summary.

Input state keys consumed:
  - incident (dict): The Incident object

Output state keys written:
  - severity_classification (str): "critical" | "warning" | "info"
  - triage_summary (str): One-sentence LLM triage reasoning
"""

import json
import logging
import os

import ollama

log = logging.getLogger("triage-agent")

# Configurable via env so users can pin a specific tag (e.g. "qwen3-coder:30b").
# `ollama list` shows available models; the bare name "qwen3-coder" works only if
# a tag-less alias exists. Default to "qwen3-coder:latest" which ollama resolves
# to the most recently pulled qwen3-coder variant.
MODEL = os.getenv("OLLAMA_MODEL", "qwen3-coder:latest")

SYSTEM_PROMPT = """\
You are a senior Site Reliability Engineer performing incident triage.

Given an Incident JSON object, do two things:
1. Classify the severity as exactly one of: critical, warning, info
2. Write one sentence explaining why.

Respond in this exact JSON format (no markdown, no extra text):
{"severity": "<critical|warning|info>", "reasoning": "<one sentence>"}
"""


def triage_agent(state: dict) -> dict:
    """
    LangGraph node function for incident triage.

    Accepts and returns the full GraphState dict.
    Only reads `state["incident"]` and writes `severity_classification` + `triage_summary`.
    """
    incident = state.get("incident", {})
    log.info("Triage started for incident id=%s workload=%s",
             incident.get("id"), incident.get("workload"))

    # Build a compact representation — avoid sending huge log lists to the LLM
    incident_summary = {
        "workload":     incident.get("workload"),
        "namespace":    incident.get("namespace"),
        "severity":     incident.get("severity"),
        "alert_labels": incident.get("alert_labels", {}),
        # Send only the first 20 log lines to stay within context limits
        "recent_logs":  incident.get("recent_logs", [])[:20],
        "recent_metrics": incident.get("recent_metrics", {}),
    }

    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {"role": "system",  "content": SYSTEM_PROMPT},
                {"role": "user",    "content": json.dumps(incident_summary, indent=2)}
            ]
        )
        raw = response["message"]["content"].strip()

        # Parse structured response
        parsed = json.loads(raw)
        classification = parsed.get("severity", "unknown").lower()
        reasoning      = parsed.get("reasoning", "")

        # Validate classification value
        if classification not in ("critical", "warning", "info"):
            log.warning("Unexpected classification '%s' — defaulting to incident.severity", classification)
            classification = incident.get("severity", "warning")

    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON triage response — using raw text")
        # Fall back: extract first word as severity
        words = raw.lower().split()
        classification = next((w for w in words if w in ("critical", "warning", "info")), incident.get("severity", "warning"))
        reasoning = raw
    except Exception as exc:
        log.error("Triage LLM call failed: %s", exc)
        classification = incident.get("severity", "warning")
        reasoning      = f"LLM unavailable: {exc}"

    log.info("Triage result: severity_classification=%s", classification)

    state["severity_classification"] = classification
    state["triage_summary"]          = reasoning
    return state
