# Chaos kit (MVP-0)

Fault injection for `liveprobe-target`, run with the **operator's** kubeconfig
(context `rancher-desktop` by default, override with `KUBE_CONTEXT`). LiveProbe's
own ServiceAccount is never used, and the ground truth of what was injected is
not stored anywhere LiveProbe can read.

| Command | Fault | Expected symptom |
|---|---|---|
| `chaos/inject.sh family1` | `inventory` image → unique `liveprobe/inventory:does-not-exist-<uuid>` each run | fresh ReplicaSet/pod in `ErrImagePull`/`ImagePullBackOff`; old pod keeps serving (rolling update) |
| `chaos/inject.sh family4` | Service `inventory` selector → `app=inventory-broken` | `inventory` has 0 endpoints; `order` returns 502 |
| `chaos/recover.sh` | re-applies target manifests from `deploy/overlays/rancher-desktop` | overlay-pinned inventory image, selector `app=inventory`, endpoints > 0, order 200 |

`recover.sh` exits non-zero and dumps state if the baseline is not reached
within `RECOVER_TIMEOUT` seconds (default 240).

Family 2 (resource/OOM) and Family 3 (cascading latency) are out of MVP-0 scope.

## KAN-71 approved v1 catalog runner

`python3 chaos/validate.py` checks the 60 approved JSON-subset YAML cases (15 per
Family). The v1 operator-only runner handles the 16 `bad_image` and
`selector_mismatch` cases. The other 44 target-API cases have separate KAN-96
live evidence; this runner does not replay those faults or silently map them to
Kubernetes writes.

From the repository root, first list without Kubernetes access:

    python3 chaos/run-case.py --context rancher-desktop --namespace liveprobe-target --evidence .e2e-tmp/KAN-71

In an exclusive, authorized target fault window, with the default profile and
private backup already checked, opt in explicitly:

    python3 chaos/run-case.py --context rancher-desktop --namespace liveprobe-target --evidence .e2e-tmp/KAN-71 --execute

The evidence directory must be mode 0700. The runner uses a local advisory lock,
fsynced `active.json` snapshot, node stop gate and per-case `finally` restore;
never start a second runner on another machine. If `active.json` remains, do not
remove it blindly: inspect the saved image/selector and verify both services'
health before any retry. It stops at the first failed signal or recovery.
Results append to `results.jsonl` (including failed attempts), never to KAN-96
history. To combine the latest owner-specific records without altering either:

    python3 chaos/tally.py --target-results .e2e-tmp/KAN-96-verify/results.jsonl --runner-results .e2e-tmp/KAN-71/results.jsonl

The catalog and answer labels stay on the operator host. Do not deploy or mount
`chaos/cases/` into a workload. See `docs/evidence/KAN-71/live-run.md` for the
actual run, initial failed attempt, restoration checks and limitations.

