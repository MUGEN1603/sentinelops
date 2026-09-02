# Codebase Concerns

**Analysis Date:** 2026-07-24

---

## Tech Debt

### [SQLite Checkpointer Not Production-Ready]

- **Issue**: The LangGraph checkpoint persistence uses `SqliteSaver` with a local SQLite file (`sentinelops_checkpoints.db`). This is single-node only and does not support horizontal scaling of the agent pipeline.
- **Files**: `agents/checkpointer_config.py:65-73`, `agents/graph.py:96-100`
- **Impact**: If the webhook server scales to multiple replicas, each will have its own checkpoint DB. State cannot be shared across instances. Process crash on a single node loses in-flight incidents unless the same thread_id is re-routed to the same pod.
- **Fix Approach**: Replace `SqliteSaver` with `PostgresSaver` from `langgraph-checkpoint-postgres` for production. The checkpointer_config.py already documents this migration path (lines 57-61).

### [Ollama Single-Threaded Concurrency Bottleneck]

- **Issue**: Ollama runs single-threaded by default. The incident normalizer uses a `ThreadPoolExecutor` with `PIPELINE_WORKERS=2` (default), but all workers contend for the same Ollama process.
- **Files**: `incident-normalizer/webhook_server.py:53-57`, `agents/triage_agent.py:28`, `agents/diagnosis_agent.py:32`, `agents/remediation_agent.py:29`
- **Impact**: Under alert bursts, pipeline latency grows linearly with queue depth. No backpressure or rejection strategy when the pool is saturated.
- **Fix Approach**: Configure Ollama with `OLLAMA_NUM_PARALLEL` > 1, or deploy multiple Ollama instances behind a load balancer. Add circuit breaker pattern to reject new incidents when queue is full.

### [Hardcoded Repository Placeholders in GitOps Manifests]

- **Issue**: The `gitops/manifests/argocd-app.yaml` contains `SENTINELOPS_REPO_OWNER` and `SENTINELOPS_REPO_NAME` placeholders that must be manually replaced before applying. The `gitops_bridge.py` also has fallback logic checking for these placeholders.
- **Files**: `agents/gitops_bridge.py:45-50`, `gitops/manifests/argocd-app.yaml`
- **Impact**: Manual step required for every new deployment. Error-prone if missed.
- **Fix Approach**: Use Helm charts or Kustomize for the Argo CD Application with values injected from CI/CD. Remove placeholder-checking logic from `gitops_bridge.py` and fail fast with clear error if `GITHUB_REPO` env var is not set.

### [Hardcoded File Path in Policy Review Agent]

- **Issue**: The `policy_review_agent.py` hardcodes the GitOps manifest path: `file_path = "gitops/manifests/sample-app-deployment.yaml"`. This only works for the single sample app.
- **Files**: `agents/policy_review_agent.py:151`
- **Impact**: Cannot remediate other workloads without code changes. The incident's `workload`/`namespace` fields are ignored for file path resolution.
- **Fix Approach**: Implement a mapping from `(namespace, workload)` → GitOps manifest path, or use a convention like `gitops/manifests/{namespace}/{workload}.yaml`.

### [Incomplete Incident Context Enrichment]

- **Issue**: The incident normalizer sets `k8s_objects: []` and `trace_refs: []` as empty lists with TODO comments to extend.
- **Files**: `incident-normalizer/webhook_server.py:286-287`
- **Impact**: Diagnosis agent lacks pod spec, deployment config, events, and distributed traces for RCA. RAG retrieval quality suffers without this context.
- **Fix Approach**: Add Kubernetes API client calls to fetch Pod/Deployment specs and recent events. Integrate with OTel collector for trace correlation.

---

## Known Bugs

### [SqliteSaver Context Manager Bug - WORKAROUND IN PLACE]

- **Symptoms**: `SqliteSaver.from_conn_string()` returns a `_GeneratorContextManager`, not a `SqliteSaver` instance. Passing it to `workflow.compile(checkpointer=...)` causes `AttributeError: '_GeneratorContextManager' object has no attribute 'put'`.
- **Files**: `agents/checkpointer_config.py:11-21` (docstring explains the bug), `agents/checkpointer_config.py:65-73` (workaround)
- **Current Workaround**: Open `sqlite3.Connection` directly with `check_same_thread=False` and pass to `SqliteSaver(conn)`. This works but holds a connection for the process lifetime without proper cleanup.
- **Trigger**: Any graph invocation using the checkpointer.
- **Fix Approach**: Use `langgraph-checkpoint-postgres` for production. For SQLite, implement proper connection lifecycle management with `try/finally` or context manager.

### [Diagnosis Agent Metrics Sanitization Logic Bug]

- **Symptoms**: Line 136 references `system_prompt` before assignment on line 92-95. The variable `system_prompt` is defined as `SYSTEM_PROMPT_TEMPLATE.format(...)` on line 92, but the metrics note is appended to `system_prompt` on line 136 *after* it was already used in the `ollama.chat()` call on line 140-146.
- **Files**: `agents/diagnosis_agent.py:92-95, 134-146`
- **Impact**: The "incomplete metrics" warning note is never actually sent to the LLM. The LLM sees incomplete metrics without knowing which ones failed.
- **Trigger**: When `fetch_recent_metrics` returns partial results (some metrics fail).
- **Fix Approach**: Move the `metrics_note` append logic before the `ollama.chat()` call, or restructure to build the final prompt in one place.

### [Remediation Agent Shell Injection Check Bypassable]

- **Symptoms**: The shell injection check (line 112-117) only looks for lowercase substrings like `"kubectl"`, `"bash"`, `"sh -c"`, etc. An LLM could embed commands in YAML anchors, use obfuscation (`kubect\l`), or use alternative shells (`zsh`, `dash`).
- **Files**: `agents/remediation_agent.py:111-117`
- **Impact**: Malicious or hallucinated LLM output could inject shell commands into the patch YAML, which would then be committed to Git and potentially executed by a compromised CI/CD runner.
- **Trigger**: LLM returns a patch containing shell command strings.
- **Fix Approach**: Use a strict YAML parser to validate the patch is a valid Kubernetes strategic-merge patch (dict with `apiVersion`, `kind`, `metadata`, `spec`). Reject any patch that doesn't parse as valid K8s YAML.

### [Policy Review Agent Silent Qdrant Failure]

- **Symptoms**: The `store_incident` call in `policy_review_agent.py:182-206` is wrapped in a bare `except Exception` that logs a warning and continues. Failed incident storage means no RAG retrieval for future similar incidents.
- **Files**: `agents/policy_review_agent.py:205-206`
- **Impact**: Silent degradation of the memory system. No alerting, no retry, no dead-letter queue.
- **Trigger**: Qdrant unavailable, network partition, embedding model error.
- **Fix Approach**: Add retry with exponential backoff. On persistent failure, write to a local dead-letter file and alert. Make Qdrant storage a hard requirement (fail the pipeline) or implement a durable outbox pattern.

---

## Security Considerations

### [OPA Runs Unauthenticated in Production Configuration]

- **Risk**: The OPA server is deployed without authentication (`--authentication=token` not enabled). Any pod with network access to port 8181 can query or modify policies.
- **Files**: `infra/network-policies.yaml:178-196` (allows egress to OPA), `policies/remediation.rego` (policy logic), `agents/policy_review_agent.py:34` (OPA_URL default)
- **Current Mitigation**: Network policies restrict egress to OPA from specific pods. `infra/secrets.yaml:68-84` documents enabling OPA auth but it's not implemented.
- **Impact**: If an attacker compromises any pod in the `observability` or `default` namespace, they can disable policy gates, approve malicious remediations, or exfiltrate policy logic.
- **Recommendations**: Enable OPA bearer token auth in production. Store token in `opa-auth-token` secret and update `policy_review_agent.py` to send `Authorization: Bearer <token>` header.

### [GitHub Token Stored as Plain Text in Kubernetes Secret]

- **Risk**: The `sentinelops-agent-secrets` secret stores `GITHUB_TOKEN` in `stringData` (base64-encoded only, not encrypted at rest).
- **Files**: `infra/secrets.yaml:42-45`
- **Current Mitigation**: README and secret template recommend fine-grained PAT with 30-day expiry and repo-scoped permissions.
- **Impact**: If etcd is compromised or secret is logged, the token grants write access to the GitOps repo. Argo CD sync could deploy malicious manifests.
- **Recommendations**: Use External Secrets Operator with a secrets backend (AWS Secrets Manager, HashiCorp Vault, Azure Key Vault). Or use Bitnami Sealed Secrets to encrypt the secret before committing to Git.

### [Slack Webhook URL in Plain Text Secret]

- **Risk**: `SLACK_WEBHOOK_URL` stored in same secret as GitHub token. If leaked, allows posting to the Slack channel.
- **Files**: `infra/secrets.yaml:45`, `agents/policy_review_agent.py:35, 95-110`
- **Impact**: Lower severity than GitHub token, but could be used for phishing or noise injection in alerting channels.
- **Recommendations**: Same as above — use External Secrets Operator. Rotate webhook URLs periodically.

### [No Mutual TLS Between Components]

- **Risk**: All inter-service communication (Qdrant, OPA, Ollama, Loki, Prometheus, GitHub API) uses plain HTTP/HTTPS without mTLS. Service-to-service authentication is absent.
- **Files**: `infra/network-policies.yaml` (only L3/L4 allowlists), `memory/qdrant_client.py:36`, `agents/policy_review_agent.py:34`, `incident-normalizer/webhook_server.py:46, 129`
- **Impact**: Network policy bypass (e.g., compromised pod in allowed namespace) allows impersonation of any component. Man-in-the-middle on pod network could modify remediation patches or OPA decisions.
- **Recommendations**: Deploy a service mesh (Istio, Linkerd, Cilium) for mTLS. Or configure each component with TLS certs and mutual authentication.

### [OPA Deny Reasons Exposed in Logs]

- **Risk**: The `policy_review_agent.py` logs OPA `deny_reason` messages (line 136-137) which include the exact policy logic that caused denial. This could help an attacker understand policy boundaries.
- **Files**: `agents/policy_review_agent.py:136-137`
- **Impact**: Information disclosure. Low severity but violates least-privilege logging principle.
- **Recommendations**: Log only `denied=true` with incident ID. Store full reasons in a secure audit log with restricted access.

### [Sample App Runs Without readOnlyRootFilesystem]

- **Risk**: The `sample-app-deployment.yaml` does not set `securityContext.readOnlyRootFilesystem: true`. The container runs as non-root (UID 1000) but can write to its filesystem.
- **Files**: `gitops/manifests/sample-app-deployment.yaml` (check for securityContext), `sample-app/Dockerfile:12-20`
- **Impact**: If the app is compromised via RCE, attacker can write payloads to disk for persistence.
- **Recommendations**: Add `readOnlyRootFilesystem: true` and `emptyDir` volumes for `/tmp` and any writable paths.

---

## Performance Bottlenecks

### [No Retry or Circuit Breaker for External Dependencies]

- **Problem**: All external calls (Ollama, OPA, GitHub API, Qdrant, Loki, Prometheus) use bare `requests` or `ollama.chat()` with fixed timeouts but no retry logic, exponential backoff, or circuit breaker.
- **Files**: 
  - `agents/triage_agent.py:65-71`, `agents/diagnosis_agent.py:140-146`, `agents/remediation_agent.py:96-102` (Ollama)
  - `agents/policy_review_agent.py:52-56, 65-69` (OPA)
  - `agents/gitops_bridge.py:90-99, 102-117, 143-151` (GitHub API)
  - `memory/qdrant_client.py:136-146, 169-173` (Qdrant)
  - `incident-normalizer/webhook_server.py:86-90, 149-153` (Loki, Prometheus)
- **Impact**: Transient network blips cause pipeline failures. No graceful degradation. Under load, cascading timeouts amplify latency.
- **Recommendations**: Add `tenacity` or `httpx` with retry policies. Implement circuit breaker (e.g., `pybreaker`) for each external dependency.

### [Ollama Model Loading Latency on Cold Start]

- **Problem**: The `ollama.chat()` calls load the model into VRAM on first request if not already loaded. With `qwen3-coder:latest` (~15GB), cold start can take 10-30 seconds.
- **Files**: `agents/triage_agent.py:28`, `agents/diagnosis_agent.py:32`, `agents/remediation_agent.py:29`
- **Impact**: First incident after Ollama restart experiences high latency. Pipeline timeout may fire.
- **Recommendations**: Pre-load model with `ollama run qwen3-coder` on startup. Set `OLLAMA_KEEP_ALIVE=-1` to keep model in memory. Configure health check that warms the model.

### [Qdrant Retrieval Blocking in Diagnosis Agent]

- **Problem**: `diagnosis_agent.py` calls `retrieve_similar()` synchronously (line 79) before LLM call. Qdrant query latency adds to pipeline latency.
- **Files**: `agents/diagnosis_agent.py:78-80`
- **Impact**: Adds ~100-500ms per incident. Not significant alone but adds up with LLM latency.
- **Recommendations**: Consider async Qdrant client or pre-fetch similar incidents in parallel with triage agent.

### [Prometheus Query Timeout Too Short for Large Clusters]

- **Problem**: `fetch_recent_metrics` uses 3-second timeout per query (line 152). In larger clusters with many series, PromQL queries can exceed this.
- **Files**: `incident-normalizer/webhook_server.py:149, 152`
- **Impact**: Metrics marked as failed, reducing RCA quality. Silent degradation.
- **Recommendations**: Increase timeout to 10-15s. Add query timeout configuration via env var.

---

## Fragile Areas

### [GraphState TypedDict with total=False Allows Silent Key Errors]

- **Problem**: `GraphState` (contracts.py:92-123) uses `TypedDict, total=False` making all keys optional. LangGraph merges node returns, but if a node forgets to write an expected key, downstream nodes get `KeyError` or `None` silently.
- **Files**: `agents/contracts.py:92-123`, all agent files reading state keys
- **Impact**: Runtime errors only caught in integration tests. No compile-time or static analysis verification of state contracts.
- **Safe Modification**: Add runtime validation in each agent: `assert "expected_key" in state` at start of node function. Consider migrating to Pydantic model for state with required fields.

### [ThreadPoolExecutor Bounded but No Rejection Handling]

- **Problem**: `webhook_server.py` uses `ThreadPoolExecutor(max_workers=2)` (line 54-57). When pool is saturated, `submit()` blocks indefinitely. No queue depth limit, no rejection response to AlertManager.
- **Files**: `incident-normalizer/webhook_server.py:53-57, 295`
- **Impact**: Under sustained alert burst, webhook handler threads block, FastAPI event loop stalls, health checks fail, AlertManager retries amplify load.
- **Safe Modification**: Use `ThreadPoolExecutor` with a bounded `queue.Queue` and custom `RejectedExecutionHandler`, or return HTTP 503 when pool is full.

### [SQLite check_same_thread=False Not Truly Thread-Safe]

- **Problem**: The checkpointer opens SQLite with `check_same_thread=False` (line 70) to allow multi-threaded access from the webhook server's thread pool. However, SQLite with this setting is not fully thread-safe for concurrent writes; it can corrupt the database under contention.
- **Files**: `agents/checkpointer_config.py:65-73`
- **Impact**: Rare but possible database corruption under high concurrency. LangGraph's checkpointer is not designed for multi-threaded writers to the same connection.
- **Safe Modification**: Use one checkpointer per thread, or use a connection pool with serialized access. Better: migrate to PostgresSaver.

### [Auto-Merge Requires Manual Branch Protection Configuration]

- **Problem**: `gitops_bridge.py` auto-merges low-risk PRs (lines 154-162) but this requires GitHub branch protection rules to be configured manually (require PR reviews, status checks, etc.). If not configured, auto-merge bypasses all gates.
- **Files**: `agents/gitops_bridge.py:39, 154-162`
- **Impact**: Misconfiguration allows unreviewed changes to merge directly to main.
- **Safe Modification**: Add startup validation that checks branch protection rules via GitHub API. Fail fast if not configured.

### [Remediation Agent Risk Level Logic Inconsistent]

- **Problem**: The `RISK_LEVELS` dict (lines 56-64) maps known patch types to risk levels, but the LLM can return any `patch_type` string. Unknown types fall back to LLM's `risk_level` (line 120) which could be "low" for a dangerous patch.
- **Files**: `agents/remediation_agent.py:56-64, 119-120`
- **Impact**: LLM hallucination could produce a "high" risk patch labeled as "low", bypassing OPA policy gate.
- **Safe Modification**: Default unknown patch types to "high" risk. Validate `patch_type` against allowlist. Reject if not in `RISK_LEVELS`.

### [Diagnosis Agent Confidence Score Not Used for Gating]

- **Problem**: The `rca_confidence` (0.0-1.0) is stored in state but never used to gate remediation. Low-confidence RCA still proceeds to remediation and policy review.
- **Files**: `agents/diagnosis_agent.py:156, 171`, `agents/contracts.py:102`, `agents/remediation_agent.py` (does not read `rca_confidence`)
- **Impact**: Low-quality RCA can produce incorrect remediation, wasting PR review cycles or creating risky patches.
- **Safe Modification**: Add confidence threshold in `policy_review_agent` or `remediation_agent`. If `rca_confidence < 0.5`, set `policy_allowed=false` with reason "Low confidence RCA".

---

## Scaling Limits

### [Single-Cluster, Single-Tenant Architecture]

- **Current Capacity**: Designed for one kind cluster with one Argo CD instance managing one GitOps repo.
- **Limit**: The `incident-normalizer` webhook server, agent pipeline, and Qdrant are all singletons. No multi-tenancy isolation (no tenant ID in incident, no namespace-scoped Qdrant collections).
- **Files**: All agent files, `incident-normalizer/webhook_server.py`, `memory/qdrant_client.py`
- **Scaling Path**: Deploy separate SentinelOps stacks per cluster/tenant. Or add `tenant_id` to all contracts, Qdrant payload, and OPA input. Implement namespace-scoped network policies and RBAC.

### [Argo CD Self-Heal Sync Interval ~3 Minutes]

- **Limit**: Argo CD's `selfHeal` uses the application controller's sync loop (default 3 min). Drift detection and remediation PR merge-to-sync latency is 3-6 minutes minimum.
- **Files**: `docs/runbook.md` (referenced), `gitops/manifests/argocd-app.yaml`
- **Impact**: Not suitable for sub-minute MTTR requirements.
- **Scaling Path**: Reduce `syncPolicy.automated.selfHeal` interval via Argo CD config (controller `--app-resync-period`). Or use Argo CD Notifications + webhook for event-driven sync.

### [Qdrant Single-Node Embedded Storage]

- **Limit**: Qdrant runs as a single Docker container with local volume (`qdrant_storage`). No replication, no horizontal scaling.
- **Files**: `docker run ... qdrant/qdrant` (README.md:42), `memory/qdrant_client.py:36`
- **Impact**: Memory/CPU bound by single node. No HA. Collection loss if container dies without volume persistence.
- **Scaling Path**: Deploy Qdrant in clustered mode (3+ nodes) with replication factor. Use Qdrant Cloud or Kubernetes operator.

---

## Dependencies at Risk

### [Ollama / qwen3-coder Model Availability]

- **Risk**: Ollama is a local LLM runner. `qwen3-coder` model must be manually pulled (`ollama pull qwen3-coder`). No automated model management. Model updates could break prompt compatibility.
- **Files**: All agents use `OLLAMA_MODEL` env var (default `qwen3-coder:latest`)
- **Impact**: If model is deleted or Ollama upgrades break API compatibility, entire pipeline fails.
- **Migration Plan**: Pin exact model digest (e.g., `qwen3-coder:30b-v1.5`). Implement model health check endpoint. Consider migrating to vLLM or TGI for production serving with model versioning.

### [LangGraph Checkpoint API Stability]

- **Risk**: LangGraph 1.x checkpoint API (`SqliteSaver`, `PostgresSaver`) has changed between minor versions. The `from_conn_string()` context manager bug (checkpointer_config.py) is an example.
- **Files**: `agents/checkpointer_config.py`, `agents/graph.py`
- **Impact**: Upgrading LangGraph may break checkpoint persistence silently.
- **Migration Plan**: Pin `langgraph-checkpoint-sqlite==3.1.0` in requirements.txt (already pinned). Add integration test that verifies checkpoint save/load across version upgrades.

### [OPA Rego Policy Version]

- **Risk**: Policy uses `import future.keywords.if` and `in` (lines 15-16) requiring OPA v0.59+. If OPA version drifts, policy may fail to load.
- **Files**: `policies/remediation.rego:15-16`
- **Impact**: OPA upgrade/downgrade breaks policy evaluation.
- **Migration Plan**: Pin OPA version in Docker/Helm values. Add CI step that runs `opa test` against the target OPA version.

---

## Missing Critical Features

### [No Dead Letter Queue for Failed Pipeline Executions]

- **Problem**: If the LangGraph pipeline crashes mid-execution (OOM, OPA timeout, GitHub API error), the incident is lost. No retry, no alerting, no manual recovery UI.
- **Files**: `incident-normalizer/webhook_server.py:220-221` (bare except logs error and returns)
- **Impact**: Silent data loss. On-call engineer unaware incident was dropped.
- **Priority**: High
- **Fix Approach**: Persist incident to durable queue (Redis, PostgreSQL, or file) before dispatch. Add retry worker with exponential backoff. Expose admin API to list/replay failed incidents.

### [No Structured Metrics / Observability for the Agents Themselves]

- **Problem**: The agents emit unstructured logs but no Prometheus metrics (latency, error rates, queue depth, LLM token usage).
- **Files**: All agent files use `logging` only.
- **Impact**: Cannot alert on pipeline degradation. Cannot measure MTTR components (triage time, diagnosis time, etc.).
- **Priority**: Medium
- **Fix Approach**: Add `prometheus-client` counters/histograms to each agent. Expose `/metrics` endpoint on agent process (or sidecar).

### [No Integration Test for Full Pipeline]

- **Problem**: `tests/test_selfheal.py` tests Argo CD drift detection only. `tests/test_state_durability.py` tests checkpointer with mocked LLM/OPA/Qdrant. No test runs the full pipeline: webhook → normalizer → graph → OPA → GitHub PR → Argo CD sync.
- **Files**: `tests/test_selfheal.py`, `tests/test_state_durability.py`, `tests/test_incident_replay.py`
- **Impact**: Regressions in agent integration only caught in production.
- **Priority**: High
- **Fix Approach**: Add CI job that spins up kind cluster, deploys all components, fires a real AlertManager webhook, and asserts PR created + Argo CD sync.

### [No Chaos Testing / Fault Injection Automation]

- **Problem**: Fault injection is manual (curl `/crash`, `/leak-memory`). No automated chaos experiments validating the detection→remediation loop.
- **Files**: `sample-app/app.py:41-58`
- **Impact**: No confidence that the system handles real failure modes (network partition, node loss, OOM, CPU throttle).
- **Priority**: Medium
- **Fix Approach**: Integrate LitmusChaos or Chaos Mesh. Define experiments: pod kill, network latency, node drain, disk pressure. Run in CI nightly.

---

## Test Coverage Gaps

### [Unit Test Coverage Threshold Only 50%]

- **Problem**: CI enforces `--cov-fail-under=50` (`.github/workflows/ci.yaml:71`). Critical paths (agent logic, graph compilation, OPA policy) may be untested.
- **Files**: `.github/workflows/ci.yaml:71`, `tests/`
- **Impact**: Refactoring agents risks silent regressions.
- **Priority**: Medium
- **Fix Approach**: Raise threshold to 80%. Add unit tests for each agent node function with mocked dependencies.

### [No Contract Tests Between Agents]

- **Problem**: Agents communicate via `GraphState` dict. No schema validation at runtime. A change to `contracts.py` that breaks an agent's expected keys will only fail at integration test time.
- **Files**: `agents/contracts.py`, all agent files
- **Impact**: Brittle integration. Hard to evolve agents independently.
- **Priority**: Medium
- **Fix Approach**: Add Pydantic validation at each node entry/exit. Or use LangGraph's `StateGraph` schema validation (available in newer versions).

### [OPA Policy Tests Only Cover Happy Path + Basic Deny]

- **Problem**: `policies/remediation_test.rego` tests allow/deny for known actions but does not test edge cases: malformed input, partial input, namespace case sensitivity, wildcard namespace matching.
- **Files**: `policies/remediation_test.rego`
- **Impact**: Policy logic bugs not caught.
- **Priority**: Low
- **Fix Approach**: Add property-based tests (using OPA's test framework) for input validation edge cases.

---

*Concerns audit: 2026-07-24*