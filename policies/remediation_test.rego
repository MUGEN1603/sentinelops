## OPA Unit Tests for sentinelops.remediation policy
## Run: opa test policies/remediation.rego policies/remediation_test.rego -v
##
## All tests must pass before loading the policy into the OPA server.

package sentinelops.remediation

import future.keywords.if

# ── ALLOW cases ───────────────────────────────────────────────────────────────

test_allow_patch_memory_medium_apps if {
    allow with input as {
        "action":     "patch_memory_limit",
        "risk_level": "medium",
        "namespace":  "apps"
    }
}

test_allow_patch_cpu_low_apps if {
    allow with input as {
        "action":     "patch_cpu_limit",
        "risk_level": "low",
        "namespace":  "apps"
    }
}

test_allow_scale_replicas_low_production if {
    allow with input as {
        "action":     "scale_replicas",
        "risk_level": "low",
        "namespace":  "production"
    }
}

test_allow_restart_annotation_low if {
    allow with input as {
        "action":     "add_restart_annotation",
        "risk_level": "low",
        "namespace":  "staging"
    }
}

# ── DENY: high risk ───────────────────────────────────────────────────────────

test_deny_high_risk_memory_patch if {
    not allow with input as {
        "action":     "patch_memory_limit",
        "risk_level": "high",
        "namespace":  "apps"
    }
}

test_deny_high_risk_scale if {
    not allow with input as {
        "action":     "scale_replicas",
        "risk_level": "high",
        "namespace":  "apps"
    }
}

# ── DENY: protected namespaces ────────────────────────────────────────────────

test_deny_kube_system if {
    not allow with input as {
        "action":     "patch_memory_limit",
        "risk_level": "low",
        "namespace":  "kube-system"
    }
}

test_deny_argocd_namespace if {
    not allow with input as {
        "action":     "patch_memory_limit",
        "risk_level": "low",
        "namespace":  "argocd"
    }
}

test_deny_kube_public if {
    not allow with input as {
        "action":     "scale_replicas",
        "risk_level": "low",
        "namespace":  "kube-public"
    }
}

# ── DENY: unknown action type ─────────────────────────────────────────────────

test_deny_unknown_action_shell_command if {
    not allow with input as {
        "action":     "kubectl_exec",
        "risk_level": "low",
        "namespace":  "apps"
    }
}

test_deny_unknown_action_delete_pod if {
    not allow with input as {
        "action":     "delete_pod",
        "risk_level": "low",
        "namespace":  "apps"
    }
}

# ── DENY: missing fields ──────────────────────────────────────────────────────

test_deny_missing_namespace if {
    not allow with input as {
        "action":     "patch_memory_limit",
        "risk_level": "low"
    }
}

test_deny_missing_risk_level if {
    not allow with input as {
        "action":     "patch_memory_limit",
        "namespace":  "apps"
    }
}

# ── deny_reason content tests ─────────────────────────────────────────────────

test_deny_reason_high_risk if {
    deny_reason["High risk changes require human approval"] with input as {
        "action":     "patch_memory_limit",
        "risk_level": "high",
        "namespace":  "apps"
    }
}

test_deny_reason_protected_namespace if {
    deny_reason["Protected namespace cannot be auto-patched"] with input as {
        "action":     "patch_memory_limit",
        "risk_level": "low",
        "namespace":  "kube-system"
    }
}

test_deny_reason_unknown_action if {
    deny_reason["Unknown action type — only known actions can be auto-remediated"] with input as {
        "action":     "something_dangerous",
        "risk_level": "low",
        "namespace":  "apps"
    }
}
