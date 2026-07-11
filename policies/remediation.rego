## OPA Policy: sentinelops/remediation
## Loaded via: curl -X PUT --data-binary @policies/remediation.rego http://localhost:8181/v1/policies/remediation
##
## Policy gate logic:
##   allow = true  iff:
##     1. action is a known remediable action type
##     2. risk_level is NOT "high"
##     3. namespace is NOT a protected namespace
##
## Testing:
##   opa test policies/remediation.rego policies/remediation_test.rego -v

package sentinelops.remediation

import future.keywords.if
import future.keywords.in

# ── Default: deny unless explicitly allowed ────────────────────────────────────
default allow := false

# ── Allow rule ────────────────────────────────────────────────────────────────
allow if {
    is_known_action(input.action)
    input.risk_level != "high"
    not is_protected_namespace(input.namespace)
}

# ── Known safe action types ───────────────────────────────────────────────────
known_actions := {
    "patch_memory_limit",
    "patch_cpu_limit",
    "scale_replicas",
    "add_restart_annotation",
}

is_known_action(action) if {
    action in known_actions
}

# ── Protected namespaces (never auto-remediate) ───────────────────────────────
protected_namespaces := {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "argocd",
    "cert-manager",
}

is_protected_namespace(ns) if {
    ns in protected_namespaces
}

# ── Deny reasons (returned alongside allow=false for observability) ───────────
deny_reason["High risk changes require human approval"] if {
    input.risk_level == "high"
}

deny_reason["Unknown action type — only known actions can be auto-remediated"] if {
    not is_known_action(input.action)
}

deny_reason["Protected namespace cannot be auto-patched"] if {
    is_protected_namespace(input.namespace)
}

deny_reason["Missing required input fields"] if {
    not input.action
}

deny_reason["Missing required input fields"] if {
    not input.risk_level
}

deny_reason["Missing required input fields"] if {
    not input.namespace
}
