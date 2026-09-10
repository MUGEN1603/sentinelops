"""
agents/remediation_agent.py — LangGraph node: remediation_agent

Responsibility:
  Propose a Kubernetes manifest patch that fixes the incident.
  The patch MUST be a YAML diff — never a shell command, never `kubectl exec`.
  Derive the risk level from the patch type.

Input state keys consumed:
  - incident (dict): The Incident object
  - rca (str): Root cause analysis from diagnosis_agent

Output state keys written:
  - proposed_patch (str): YAML diff / strategic-merge patch content
  - patch_type (str): e.g. "patch_memory_limit", "scale_replicas", "restart_pod"
  - risk_level (str): "low" | "medium" | "high"
  - rollback_hint (str): How to undo the proposed change
"""

import json
import logging

# Use the robust LLM client with circuit breaker, retries, and fallback
from agents.llm_client import get_llm_client

log = logging.getLogger("remediation-agent")

# Model config is now handled by the LLMClient with circuit breaker and fallback
# OLLAMA_MODEL, OLLAMA_FALLBACK_MODEL, OLLAMA_TERTIARY_MODEL env vars control the chain

SYSTEM_PROMPT = """\
You are a Kubernetes remediation engineer.

Given an incident's root cause analysis, propose a REMEDIATION using a Kubernetes manifest patch.

Rules:
  1. NEVER propose a shell command, kubectl exec, or any imperative action.
  2. The patch MUST be expressed as a YAML strategic-merge patch or JSON patch.
  3. Choose the MOST CONSERVATIVE patch that fixes the problem.
  4. Derive risk_level from the patch type:
       low    = restart annotation bump (safe rollout), minor label change
       medium = resource limit/request change, replica count change
       high   = image change, security context change, network policy change

Respond in this exact JSON format (no markdown, no extra text):
{
  "patch_type": "<patch_memory_limit|scale_replicas|add_restart_annotation|other>",
  "risk_level": "<low|medium|high>",
  "target_resource": "<namespace/kind/name>",
  "patch_yaml": "<the full YAML strategic-merge patch content as a string>",
  "rollback_hint": "<one sentence on how to revert this change>"
}
"""

# ── Risk level lookup for known patch types (override LLM if mismatch) ───────
RISK_LEVELS = {
    "add_restart_annotation": "low",
    "patch_memory_limit":     "medium",
    "scale_replicas":         "medium",
    "patch_cpu_limit":        "medium",
    "change_image":           "high",
    "patch_network_policy":   "high",
    "patch_security_context": "high",
}


async def remediation_agent(state: dict) -> dict:
    """
    LangGraph node function for remediation proposal.

    Reads incident and rca from state.
    Asks the LLM for a YAML patch. Never accepts shell commands.
    Writes proposed_patch, patch_type, risk_level, and rollback_hint to state.
    """
    incident = state.get("incident", {})
    rca      = state.get("rca", "No RCA available")

    log.info("Remediation started for incident id=%s", incident.get("id"))

    # Strip _fetch_errors sentinel key injected by fetch_recent_metrics on failure
    # so JSON serialisation doesn't break on non-float values
    raw_metrics = dict(incident.get("recent_metrics", {}))
    raw_metrics.pop("_fetch_errors", None)
    numeric_metrics = {k: v for k, v in raw_metrics.items() if isinstance(v, (int, float))}

    prompt_content = {
        "workload":        incident.get("workload"),
        "namespace":       incident.get("namespace"),
        "kind":            incident.get("kind", "Pod"),
        "rca_summary":     rca[:1000],   # truncate to avoid hitting context limits
        "current_metrics": numeric_metrics,
    }

    # Use the robust LLM client with circuit breaker, retries, and fallback
    llm_client = get_llm_client()

    try:
        raw = await llm_client.chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt_content, indent=2)}
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        parsed = json.loads(raw.strip())

        patch_type     = parsed.get("patch_type", "other")
        proposed_patch = parsed.get("patch_yaml", "").strip()
        rollback_hint  = parsed.get("rollback_hint", "Revert the commit in the gitops/manifests/ directory.")
        llm_risk       = parsed.get("risk_level", "medium").lower()

        # Validate: reject shell commands embedded in the patch
        shell_indicators = ["kubectl", "bash", "sh -c", "exec", "os.system", "subprocess"]
        if any(ind in proposed_patch.lower() for ind in shell_indicators):
            log.error("LLM attempted to inject shell command in patch — rejecting")
            proposed_patch = "# REJECTED: LLM proposed a shell command. Manual remediation required."
            patch_type     = "rejected"
            llm_risk       = "high"

        # Use our risk table if we recognise the patch type; trust LLM otherwise
        risk_level = RISK_LEVELS.get(patch_type, llm_risk)

    except json.JSONDecodeError:
        log.warning("Remediation LLM returned non-JSON — attempting regex extraction")
        # We can't easily get the raw response here, so use a fallback
        proposed_patch = "# REJECTED: LLM returned invalid JSON. Manual remediation required."
        patch_type     = "rejected"
        risk_level     = "high"
        rollback_hint  = "Manually revert the change via Git."
    except Exception as exc:
        log.error("Remediation LLM call failed: %s", exc)
        proposed_patch = f"# ERROR: {exc}"
        patch_type     = "error"
        risk_level     = "high"
        rollback_hint  = "No patch generated — manual remediation required."

    log.info("Remediation complete: patch_type=%s risk_level=%s", patch_type, risk_level)

    state["proposed_patch"] = proposed_patch
    state["patch_type"]     = patch_type
    state["risk_level"]     = risk_level
    state["rollback_hint"]  = rollback_hint
    return state
