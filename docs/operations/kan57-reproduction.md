# KAN-57 reproduction and operational gates

Status: procedure draft, not evidence of a fresh-cluster install or a successful four-family run. No destructive cleanup is authorized by this document.

## Deployment boundary

The existing `scripts/install.sh` defaults to `rancher-desktop` and `deploy/overlays/rancher-desktop`. It creates/updates resources in `liveprobe-system`, `liveprobe-observability`, and `liveprobe-target`, and requires an operator-provided private password file plus subscription CLI OAuth configuration. The portable-example overlay is render-only and uses placeholder registry/domain values; it is **not** a tested fresh-cluster install. `scripts/uninstall.sh` removes namespaces and must not be used without an explicit retention decision and backup validation.

For a genuinely fresh approved cluster, record its non-secret context name, Kubernetes version, architecture, available storage class/ingress, image digests and platform manifests. Inspect `scripts/preflight.py`, `scripts/create-secrets.sh`, the overlay's pinned images, and `scripts/install.sh` before applying; obtain the operator-owned credentials through the prescribed masked/vault workflow, never by displaying a Secret. Render and validate manifests with `scripts/validate-manifests.sh` and `kubectl kustomize <approved-overlay>`; use `KUBE_CONTEXT=<approved-context> OVERLAY=<approved-overlay> scripts/install.sh` only after scoped operational approval. Confirm all three namespaces' workloads Ready, `/api/readyz`, and the authenticated UI. Reapplying to an existing cluster is not evidence of a fresh install.

## Architecture (deployed intent)

```text
inventory/order/loadgen (liveprobe-target)
       | Kubernetes events + Prometheus/Alertmanager + Loki
       v
LiveProbe API (liveprobe-system) -> PostgreSQL/pgvector
       | incident -> router -> bounded agent groups -> safety executor/handoff
       |                         ^ RAG corpus/citations
       v
cookie-authenticated /api -> React dashboard; SSE /api/stream
       |
       + observability: Grafana (liveprobe-observability)
```

The optional safe-action-executor overlay is not in `deploy/base` by default. Read-only ServiceAccount permissions are distinct from optional narrowly scoped mutation grants. `scripts/rbac-matrix.sh` and `scripts/netpol-check.sh` must be rerun on the selected context; neither test implies an actual allowed mutation was exercised. Runtime labels/ground truth in `chaos/cases/LABELS.json` are evaluation-only and must not be mounted into inference containers.

For this local MVP the API Pod's outbound HTTPS to public addresses remains broadly allowed (owner decision on Jira KAN-89 #10163) so subscription CLI model calls work. This is also an exfiltration and arbitrary external-call route if the Pod is compromised; it is not a security boundary. Before a less-trusted or nonlocal install, restrict egress to provider domains via a reviewed proxy/FQDN-capable policy, test the CLI's OAuth refresh and model endpoints through it, and deny other public destinations. Do not represent the current NetworkPolicy as domain-restricted.

## Single-fault E2E gate

Before every injection: explicitly confirm context, target namespace and workload; no other fault operator; take the appropriate scoped backup; verify current image/replicas/endpoints/order HTTP baseline, Pod readiness and resource headroom. Acquire the host flock and create-only namespace Lease, and recheck ownership before mutation. Stop when either lock is unavailable. Do not start a second case while the first is active. The F2 path must pass `scripts/family2_live.py` preflight/transaction gates (do not bypass them). Record exact case ID, timestamp, deployed CLI LLM provider (`codex_cli` or `claude_cli`, never an API-key provider), model/pricing version, incident ID, route/features, agent runs, action/handoff audit, UI observation, and sanitized evidence. Compare against approved label offline, not in the inference process.

In a `finally` path for **each** case, restore only its scoped fault, verify baseline and release the Lease/host lock. On uncertain write, failed rollback, incomplete baseline or ownership loss: quarantine and stop; do not run another case. A `0` failure count is meaningful only after inspecting all actual audit/rollback outcomes. Sequence F1, F2, F3, F4 only after the preceding case passes restoration. F2 and F3 must additionally confirm fresh router decisions (KAN-136), not merely replay previous incidents. `scripts/e2e-mvp0.sh` covers only F1/F4 and must not be represented as the four-family test.

## Evidence and troubleshooting

Capture a command/version manifest and incident/route/action IDs without cookie, credentials, raw private logs or hidden labels. Check routing policy, model provider, limits, citations, audit and verified rollback before declaring success. If no new incident appears, inspect watcher/Alertmanager, then the read-only audit observer; don't repeat a live fault just to generate an incident. If SSE disconnects, reload the authenticated snapshot; if the UI is unavailable, record failure, not a screenshot substitute. If CLI subscription usage is limited, checkpoint and wait; never fall back to API keys. The cluster's Grafana provisioning and F2 observations are documented in `docs/evidence/KAN-46/`; they do not prove this new run.
