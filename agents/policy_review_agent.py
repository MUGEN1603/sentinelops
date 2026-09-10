"""
agents/policy_review_agent.py — LangGraph node: policy_review_agent

Responsibility:
  Submit the proposed remediation to OPA for policy evaluation.
  If allowed, call the GitHub PR bridge to create a remediation PR.
  If denied, log the rejection and (optionally) notify Slack.

Input state keys consumed:
  - incident (dict): The Incident object
  - proposed_patch (str): YAML patch from remediation_agent
  - patch_type (str): Patch type identifier
  - risk_level (str): "low" | "medium" | "high"
  - rca (str): Root cause analysis (used for PR description)

Output state keys written:
  - policy_allowed (bool): OPA decision
  - policy_reasons (List[str]): OPA deny reasons (empty if allowed)
  - gitops_change (dict | None): GitOpsChange dict if PR was created
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from agents.contracts import GitOpsChange

log = logging.getLogger("policy-review-agent")

OPA_URL      = os.getenv("OPA_URL", "http://localhost:8181")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")   # optional — set to notify on deny


async def query_opa(action: str, risk_level: str, namespace: str) -> tuple[bool, list[str]]:
    """
    Submit a policy query to OPA and return (allowed, deny_reasons).

    OPA endpoint: POST /v1/data/sentinelops/remediation/allow
    """
    opa_input = {
        "input": {
            "action":     action,
            "risk_level": risk_level,
            "namespace":  namespace,
        }
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                f"{OPA_URL}/v1/data/sentinelops/remediation/allow",
                json=opa_input,
            )
            resp.raise_for_status()
            allowed = bool(resp.json().get("result", False))
            log.info("OPA decision: allowed=%s action=%s risk=%s ns=%s",
                     allowed, action, risk_level, namespace)

            # Also fetch deny reasons if rejected
            reasons: list[str] = []
            if not allowed:
                deny_resp = await client.post(
                    f"{OPA_URL}/v1/data/sentinelops/remediation/deny_reason",
                    json=opa_input,
                )
                # OPA's Rego `deny_reason["..."] if {...}` produces a SET, which OPA
                # serializes as a JSON array (list). Older/non-set rule shapes may
                # produce an object (dict). Handle both so we never crash on shape.
                result = deny_resp.json().get("result", None)
                if isinstance(result, list):
                    # Sets of strings come back as a list of strings.
                    reasons = [str(r) for r in result]
                elif isinstance(result, dict):
                    reasons = list(result.keys())
                elif result is None:
                    reasons = []
                else:
                    reasons = [str(result)]

            return allowed, reasons

        except httpx.ConnectError:
            # OPA unreachable → FAIL CLOSED (deny by default)
            log.error("OPA unreachable at %s — failing closed (deny)", OPA_URL)
            return False, ["OPA server unreachable — failing closed"]
        except Exception as exc:
            log.error("OPA query failed: %s", exc)
            return False, [f"OPA query error: {exc}"]


async def notify_slack_rejection(incident_id: str, reasons: list[str]) -> None:
    """Send a Slack notification when a remediation is rejected."""
    if not SLACK_WEBHOOK:
        return
    message = {
        "text": (
            f":no_entry: *SentinelOps Remediation Rejected*\n"
            f"Incident: `{incident_id}`\n"
            f"Reasons:\n" + "\n".join(f"  • {r}" for r in reasons)
        )
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            await client.post(SLACK_WEBHOOK, json=message)
            log.info("Slack rejection notification sent for incident=%s", incident_id)
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)


async def policy_review_agent(state: dict) -> dict:
    """
    LangGraph node function for OPA policy review and PR creation.

    Query OPA with the remediation action, risk level, and namespace.
    If allowed: call the GitOps bridge to create a GitHub PR.
    If denied: log rejection, notify Slack, store reasons in state.
    """
    incident   = state.get("incident", {})
    patch_type = state.get("patch_type", "other")
    risk_level = state.get("risk_level", "high")
    namespace  = incident.get("namespace", "unknown")

    log.info("Policy review for incident=%s patch_type=%s risk=%s ns=%s",
             incident.get("id"), patch_type, risk_level, namespace)

    # ── OPA evaluation ────────────────────────────────────────────────────────
    allowed, reasons = await query_opa(patch_type, risk_level, namespace)

    state["policy_allowed"] = allowed
    state["policy_reasons"] = reasons

    if not allowed:
        log.warning("Remediation DENIED for incident=%s reasons=%s",
                    incident.get("id"), reasons)
        await notify_slack_rejection(incident.get("id", "unknown"), reasons)
        state["gitops_change"] = None
        return state

    # ── Create GitHub PR ──────────────────────────────────────────────────────
    log.info("Remediation APPROVED — creating GitHub PR for incident=%s", incident.get("id"))

    try:
        from agents.gitops_bridge import open_remediation_pr

        # GITHUB_REPO env var must be set; gitops_bridge will raise a clear
        # RuntimeError if it's missing rather than silently failing mid-pipeline.
        repo_name  = os.getenv("GITHUB_REPO", "")
        file_path  = "gitops/manifests/sample-app-deployment.yaml"
        new_content = state.get("proposed_patch", "")

        pr_url = await open_remediation_pr(
            repo_name=repo_name,
            incident_id=incident["id"],
            file_path=file_path,
            new_content=new_content,
            rca_summary=state.get("rca", "")[:500],
            risk_level=risk_level
        )

        gitops_change = GitOpsChange(
            branch=f"auto-fix/{incident['id']}",
            files_changed=[file_path],
            pr_url=pr_url,
            merge_status="open"
        )
        state["gitops_change"] = gitops_change.model_dump()
        log.info("PR created: %s", pr_url)

    except Exception as exc:
        log.error("GitHub PR creation failed: %s", exc)
        state["gitops_change"] = {
            "branch": f"auto-fix/{incident.get('id', 'unknown')}",
            "files_changed": [],
            "pr_url": None,
            "merge_status": f"error: {exc}"
        }

    # ── Store incident in Qdrant for future RAG retrieval ─────────────────────
    try:
        # Resolve via module so pytest monkeypatching of
        # `memory.qdrant_client.store_incident` takes effect at call-time.
        from memory import qdrant_client as _qdrant_mem
        store_incident = _qdrant_mem.store_incident
        incident_text = (
            f"{incident.get('workload', '')} "
            f"{json.dumps(incident.get('alert_labels', {}))} "
            f"{' '.join(incident.get('recent_logs', [])[:10])}"
        )
        await store_incident(
            incident_id=incident["id"],
            text=incident_text,
            rca=state.get("rca", "")[:500],
            outcome="pr_created",
            metadata={
                "namespace":  namespace,
                "severity":   state.get("severity_classification", "unknown"),
                "patch_type": patch_type,
                "risk_level": risk_level,
            }
        )
        log.info("Incident stored in Qdrant for future retrieval")
    except Exception as exc:
        log.warning("Qdrant store failed (non-blocking): %s", exc)

    return state
