# LiveProbe deployment, data and permission boundaries (KAN-45)

Story KAN-45 (subtasks KAN-61, KAN-62, KAN-63). Everything here was run on the local `rancher-desktop`
context. Evidence is in `docs/evidence/KAN-45/`.

## 1. Kustomize layout (KAN-61)

```
deploy/base/                     portable manifests: core/apps/rbac/networking.k8s.io only
  network/network-policies.yaml  default-deny + explicit allow list (section 3)
deploy/components/platform/      Kustomize Component: copies overlay parameters into the base objects
deploy/overlays/rancher-desktop/ Traefik, local-path, *.localhost, local :dev images (installed)
deploy/overlays/portable-example/ render-only: nginx, standard-rwo, *.example.test, private registry
```

An overlay sets its environment in `platform.yaml`, a ConfigMap annotated
`config.kubernetes.io/local-config: "true"`, so it is used at render time and never applied:

| key | used for |
|---|---|
| `ingressClassName` | `spec.ingressClassName` of all 3 Ingresses |
| `storageClassName` | all PVCs and the postgres `volumeClaimTemplate` |
| `liveprobeHost`, `grafanaHost` | Ingress hosts |
| `ingressNamespace`, `ingressPodName` | NetworkPolicy allow-list for the ingress controller pods |
| `apiserverCIDR` | NetworkPolicy egress to the Kubernetes API (endpoint address after Service DNAT) |
| `imageRegistry` (optional) | asserted by `scripts/check-overlay.py` |

The registry and image tags are set with the overlay's `images:` list.

Commands:

```
scripts/validate-manifests.sh   # renders base + both overlays, runs kubeconform -strict, checks portability and that
                                # each render carries exactly its platform.yaml values
scripts/install.sh              # preflight -> namespaces -> secrets (create if missing) -> images -> apply -> wait Ready
SKIP_BUILD=1 scripts/install.sh # reuse local images
OVERLAY=deploy/overlays/<env> KUBE_CONTEXT=<ctx> scripts/install.sh
scripts/uninstall.sh --dry-run  # server-side dry run, lists every object that would be deleted
scripts/uninstall.sh --yes      # pg-backup.sh first, then delete the rendered namespaces + cluster RBAC
```

`scripts/preflight.py` stops the install if an overlay value doesn't match the cluster: missing
IngressClass or StorageClass, no ingress-controller pods, no `k8s-app=kube-dns` pods, or an API endpoint
outside `apiserverCIDR`. Without this check, a wrong value would cut off ingress, DNS or API access
once the default-deny policies are applied.

**Uninstall deletes data.** The three namespaces hold the PostgreSQL, Prometheus and Loki PVCs, and the
`local-path` StorageClass reclaim policy is `Delete`. `uninstall.sh --yes` therefore takes a
`pg-backup.sh` dump first unless `NO_BACKUP=1` is set. It refuses to delete a namespace labelled
`part-of` something other than `liveprobe*`.

Uninstall also deletes the Secrets. On the next install, `create-secrets.sh` generates new PostgreSQL,
admin, session, webhook and Grafana credentials. Saved dashboard or Grafana logins must be updated, and
existing sessions become invalid. `liveprobe-llm` is copied again from `~/.codex/auth.json`. A restored
dump still works because `pg_restore --no-owner` does not depend on the old DB password.

Verified cycle (2026-09-26, KAN-93 option a): fresh backup → `uninstall.sh --yes` → `SKIP_BUILD=1 install.sh`
→ live restore of the pre-delete dump → same 71 objects, second install makes no change, incidents 1–6
restored, RBAC/NetworkPolicy/E2E pass. Evidence: `docs/evidence/KAN-45/reinstall-cycle/`.

## 2. PostgreSQL + pgvector backup / restore (KAN-62)

- StatefulSet `postgres` (image `pgvector/pgvector:0.8.6-pg17-bookworm`) with a 2Gi RWO PVC
  `data-postgres-0`, `pg_isready` readiness/liveness probes, 128Mi request / 256Mi limit and no SA token.
  The password comes from Secret `postgres-credentials`, which `scripts/create-secrets.sh` generates
  locally and never prints.
- `scripts/pg-backup.sh` runs `pg_dump -Fc --no-owner --no-privileges` inside `postgres-0`, so the
  password never leaves the pod. It writes the dump to `backups/` (mode 700/600, git-ignored), along
  with `.sha256` and `.manifest.json`: extension versions, Alembic revision, exact per-table row counts
  and non-null vector counts. The archive is checked with `pg_restore --list` before it is kept.
- `scripts/pg-restore.sh <dump>` restores **only into an isolated target** and verifies the sha256,
  a clean `pg_restore --exit-on-error`, extension versions, the Alembic revision, per-table row counts,
  vector counts and a pgvector `<=>` KNN query:
  - `RESTORE_MODE=docker` (default): a throwaway container from the same image with `--network none`,
    removed afterwards.
  - `RESTORE_MODE=cluster-scratch`: `CREATE DATABASE liveprobe_restore_check_<ts>` on the live
    server, dropped afterwards. The script refuses any other name, and the live `liveprobe` DB is not
    written to.
- Data survives a pod restart: `kubectl delete pod postgres-0` gives a new pod UID with the same PVC
  UID, and row counts, Alembic revision and vector version are the same afterwards.

### Restoring the live database (manual, needs the operator's authorization; not run by any script)

1. Take a fresh `scripts/pg-backup.sh` of the current state and verify the restore candidate with
   `scripts/pg-restore.sh <candidate>`.
2. `kubectl -n liveprobe-system scale deploy/liveprobe-api --replicas=0` (stops writers).
3. Inside `postgres-0`: `dropdb liveprobe && createdb liveprobe`, then pipe the dump into
   `pg_restore -d liveprobe --no-owner --no-privileges --exit-on-error`.
4. Compare row counts against the candidate's manifest, then scale `liveprobe-api` back to 1 and run
   `python3 scripts/smoke-api.py`.

Limitations: logical dumps are point-in-time (no WAL archiving/PITR). Backups live on the operator's
machine and are unencrypted apart from file permissions. Single replica; the PVC is node-local
(`local-path`) and a node loss loses the volume. The row counts in the manifest come from a separate
REPEATABLE READ snapshot taken just before `pg_dump`, so writes between the two show up as a
verification difference; they are not hidden.

## 3. RBAC and NetworkPolicy boundary (KAN-63)

RBAC (unchanged from MVP-0, now fully covered by `scripts/rbac-matrix.sh`, 101 assertions):

| ServiceAccount | granted | verified denied |
|---|---|---|
| `liveprobe-system:liveprobe` | cluster-wide get/list/watch on pods, pods/log, events, services, endpoints, namespaces, nodes, deployments, replicasets, endpointslices; `patch,update deployments` in `liveprobe-target` only | Secrets get/list/watch in every namespace; any write outside `liveprobe-target`; create/delete deployments, pod delete/exec/eviction, service/configmap patch in target; node create/update/patch/delete and nodes/status; RBAC create/escalate/bind; impersonate; token create; `*/*` |
| `liveprobe-observability:{prometheus,alloy,kube-state-metrics}` | read-only discovery (per ClusterRole) | Secrets, workload writes, node patch, RBAC create; KSM cannot list ConfigMaps |
| `default` SA in all 3 namespaces | nothing | pods list, Secrets, deployment patch |

Pods that never call the API (`dashboard`, `postgres`, `grafana`, `loki`, `alertmanager`, `order`,
`inventory`, `loadgen`) run with `automountServiceAccountToken: false`, set by a base patch.

NetworkPolicy: `default-deny-all` (Ingress+Egress) in all three namespaces, plus these allowed flows:

- DNS: every pod may reach kube-dns on port 53.
- Ingress controller: may reach dashboard:80, liveprobe-api:8000 and grafana:3000.
- liveprobe-api: may reach postgres:5432, prometheus:9090, loki:3100 and the API server. It may also
  make HTTPS (:443) calls to public addresses (RFC1918, link-local, CGNAT and loopback excluded) for
  the LLM provider. NetworkPolicy cannot match FQDNs, so this rule cannot be narrowed to one provider.
- alertmanager: may reach liveprobe-api:8000 (webhook).
- prometheus: may reach KSM:8080, alertmanager:9093, target pods:8080 and the API server.
- grafana: may reach prometheus and loki.
- alloy: may reach loki and the API server.
- KSM: may reach the API server.
- liveprobe-target: pods may talk to each other within the namespace, and prometheus may scrape them.
  Everything else is denied.

`scripts/netpol-check.sh` opens real TCP connections from inside the pods: 45 checks covering 15
allowed flows, 27 denied flows and the 3 ingress URLs. `--observe` prints the raw outcomes. Before
the policies were applied, all 42 in-cluster flows were `open`. On K3s/kube-router a denied flow shows
up as either `blocked` (timeout) or `refused` (reject), and every denied destination has an allowed
positive control in the same run.

Follow-on stories that add traffic must add their own narrowly scoped policy and re-run the check.
Known upcoming flows: KAN-47 inventory → PostgreSQL with a dedicated DB identity, and KAN-46 extra
scrapes or webhooks.
