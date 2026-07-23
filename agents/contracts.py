"""
agents/contracts.py — Shared Pydantic data contracts for the SentinelOps pipeline.

All agents communicate exclusively via these typed schemas.
Changing a field here requires updating every agent node that reads or writes it.
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Optional, TypedDict
import datetime


class Incident(BaseModel):
    """
    The canonical incident object built by the Incident Normalizer from an
    AlertManager webhook payload, enriched with logs and metrics.
    This is the primary input to the LangGraph pipeline.
    """
    id: str = Field(..., description="UUID4 unique identifier for this incident")
    started_at: str = Field(..., description="ISO-8601 UTC timestamp when the alert fired")
    namespace: str = Field(..., description="Kubernetes namespace of the affected workload")
    workload: str = Field(..., description="Pod or Deployment name affected")
    kind: str = Field(default="Pod", description="Kubernetes resource kind: Pod, Deployment, etc.")
    severity: str = Field(..., description="Alert severity: critical | warning | info")
    alert_labels: Dict[str, str] = Field(default_factory=dict, description="Raw labels from AlertManager alert")
    recent_logs: List[str] = Field(default_factory=list, description="Last N log lines from Loki for this pod")
    recent_metrics: Dict[str, float] = Field(default_factory=dict, description="Key metrics at incident time (e.g. memory_usage_bytes)")
    metric_fetch_errors: List[str] = Field(default_factory=list, description="Metric names that failed to fetch; signals RCA context is incomplete")
    k8s_objects: List[dict] = Field(default_factory=list, description="Raw k8s object snapshots (Deployment, Pod spec, Events)")
    trace_refs: List[str] = Field(default_factory=list, description="OTel trace IDs correlated with this incident")


class RCAResult(BaseModel):
    """
    Root Cause Analysis produced by the diagnosis_agent.
    Stored in Qdrant after resolution for future RAG retrieval.
    """
    incident_id: str
    summary: str = Field(..., description="One-sentence human-readable summary")
    probable_cause: str = Field(..., description="Detailed root cause explanation")
    evidence: List[str] = Field(default_factory=list, description="Log lines, metrics, or k8s events supporting the diagnosis")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Agent confidence score between 0 and 1")
    similar_incidents: List[str] = Field(default_factory=list, description="IDs of similar past incidents retrieved from Qdrant")


class RemediationIntent(BaseModel):
    """
    A proposed remediation action produced by the remediation_agent.
    Must be a Kubernetes manifest patch — never a shell command.
    """
    type: str = Field(..., description="Remediation type: patch_memory_limit | scale_replicas | restart_pod")
    target: str = Field(..., description="Kubernetes resource path, e.g. apps/deployments/sample-app")
    patch_format: str = Field(default="strategic-merge", description="Patch format: strategic-merge | json-patch | yaml-diff")
    diff: str = Field(..., description="Full YAML diff or patch content to be applied")
    risk_level: str = Field(..., description="Risk level: low | medium | high")
    rollback_hint: str = Field(..., description="How to roll back this change if it causes further issues")


class PolicyDecision(BaseModel):
    """
    The OPA policy evaluation result from the policy_review_agent.
    """
    allowed: bool = Field(..., description="True if OPA permits this remediation to proceed")
    reasons: List[str] = Field(default_factory=list, description="Human-readable reasons for allow or deny")
    required_approvals: List[str] = Field(default_factory=list, description="GitHub users/teams required to approve before merge")
    constraints: List[str] = Field(default_factory=list, description="Additional constraints imposed by the policy")


class GitOpsChange(BaseModel):
    """
    Record of a GitOps change created by the remediation PR bridge.
    Stored in agent state and in the runbook for audit purposes.
    """
    branch: str = Field(..., description="Branch name created for this remediation, e.g. auto-fix/<incident-id>")
    files_changed: List[str] = Field(default_factory=list, description="Repository-relative paths of files modified")
    pr_url: Optional[str] = Field(default=None, description="GitHub PR URL, populated after PR is created")
    merge_status: str = Field(default="open", description="PR status: open | merged | closed | auto-merged")
    created_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat(),
        description="UTC timestamp when this PR was created"
    )


# ── Convenience type alias used in the LangGraph GraphState ──────────────────
# LangGraph 1.x requires a TypedDict (not a plain dict subclass) so the
# framework knows the state schema and can merge node return values. A plain
# `class GraphState(dict)` causes invoke() to silently return None because
# LangGraph treats it as "no schema defined". Using TypedDict with total=False
# lets every key be optional (nodes set only the keys they produce).


class GraphState(TypedDict, total=False):
    """
    The mutable state dict passed between LangGraph agent nodes.

    Expected keys after each phase:
      - incident            : Incident dict            (set by: normalizer)
      - severity_classification : str                  (set by: triage_agent)
      - triage_summary      : str                      (set by: triage_agent)
      - similar_incidents   : List[str]                (set by: diagnosis_agent)
      - rca                 : str                      (set by: diagnosis_agent)
      - rca_confidence      : float                    (set by: diagnosis_agent)
      - proposed_patch      : str                      (set by: remediation_agent)
      - patch_type          : str                      (set by: remediation_agent)
      - risk_level          : str                      (set by: remediation_agent)
      - rollback_hint       : str                      (set by: remediation_agent)
      - policy_allowed      : bool                     (set by: policy_review_agent)
      - policy_reasons      : List[str]                (set by: policy_review_agent)
      - gitops_change       : GitOpsChange dict        (set by: policy_review_agent on allow)
    """
    incident: dict
    severity_classification: str
    triage_summary: str
    similar_incidents: list
    rca: str
    rca_confidence: float
    proposed_patch: str
    patch_type: str
    risk_level: str
    rollback_hint: str
    policy_allowed: bool
    policy_reasons: list
    gitops_change: dict
