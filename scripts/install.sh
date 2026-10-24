#!/usr/bin/env bash
# One-command, idempotent install of the LiveProbe stack.
#   scripts/install.sh                                   # rancher-desktop overlay: secrets -> images -> apply -> wait
#   SKIP_BUILD=1 scripts/install.sh                      # reuse existing local :dev images
#   OVERLAY=deploy/overlays/<env> KUBE_CONTEXT=<ctx> scripts/install.sh
# Re-running converges to the same state: namespaces/objects are applied declaratively, Secrets are only
# created when missing (scripts/create-secrets.sh), and nothing is deleted.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:-rancher-desktop}"
OVERLAY="${OVERLAY:-deploy/overlays/rancher-desktop}"
K="kubectl --context ${CTX}"

echo "==> context: ${CTX}  overlay: ${OVERLAY}"
$K cluster-info >/dev/null

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
kubectl kustomize "$OVERLAY" > "$rendered"

echo "==> preflight: overlay platform values match the cluster"
python3 scripts/preflight.py --context "$CTX" --overlay "$OVERLAY"

echo "==> namespaces"
$K apply -f deploy/base/namespaces/namespaces.yaml

echo "==> secrets"
KUBE_CONTEXT="$CTX" scripts/create-secrets.sh

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "==> images"
  scripts/build-images.sh
fi

echo "==> apply ${OVERLAY}"
$K apply -f "$rendered"

echo "==> wait for rollouts"
wait_ns() {
  local ns="$1"
  for r in $($K -n "$ns" get deploy,statefulset -o name); do
    $K -n "$ns" rollout status "$r" --timeout="${ROLLOUT_TIMEOUT:-600s}"
  done
}
wait_ns liveprobe-system
echo "==> inventory database identity (KAN-47)"
KUBE_CONTEXT="$CTX" scripts/provision-inventory-db.sh
wait_ns liveprobe-observability
wait_ns liveprobe-target

echo "==> pods"
$K get pods -n liveprobe-system -o wide
$K get pods -n liveprobe-observability -o wide
$K get pods -n liveprobe-target -o wide

not_ready=$($K get pods -A -l '!job-name' --no-headers 2>/dev/null \
  | awk '$1 ~ /^liveprobe-/ { split($3,a,"/"); if (a[1]!=a[2] || $4!="Running") print $1"/"$2 }')
if [[ -n "$not_ready" ]]; then
  echo "NOT READY:"; echo "$not_ready"; exit 1
fi
echo "==> all LiveProbe pods Ready"
for ns in liveprobe-system liveprobe-observability; do
  $K -n "$ns" get ingress -o jsonpath='{range .items[*]}{"URL: http://"}{.spec.rules[0].host}{.spec.rules[0].http.paths[0].path}{"\n"}{end}'
done
