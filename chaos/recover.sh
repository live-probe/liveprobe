#!/usr/bin/env bash
# Restore the target namespace to baseline from the declarative manifests and verify it.
# Exits non-zero (with a state dump) if baseline is not reached.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS=liveprobe-target
K="kubectl --context ${CTX}"
TIMEOUT="${RECOVER_TIMEOUT:-240}"

dump() {
  echo "---- state dump ----" >&2
  $K -n $NS get deploy,rs,pods,svc,endpoints -o wide >&2 || true
  $K -n $NS get events --sort-by=.lastTimestamp 2>/dev/null | tail -20 >&2 || true
}
trap 'rc=$?; [[ $rc -ne 0 ]] && dump; exit $rc' EXIT

# Re-apply only the target manifests (overlay-rendered image digest, original selector).
kubectl kustomize deploy/overlays/rancher-desktop \
  | $K apply -f - --selector app.kubernetes.io/part-of=liveprobe-target >/dev/null
echo "re-applied target manifests"

$K -n $NS rollout status deploy/inventory --timeout=${TIMEOUT}s
$K -n $NS rollout status deploy/order --timeout=${TIMEOUT}s

want=$(chaos/expected-image.sh inventory)
img=$($K -n $NS get deploy inventory -o jsonpath='{.spec.template.spec.containers[0].image}')
sel=$($K -n $NS get svc inventory -o jsonpath='{.spec.selector.app}')
[[ "$img" == "$want" ]] || { echo "inventory image is $img (want $want)" >&2; exit 1; }
[[ "$sel" == "inventory" ]] || { echo "inventory selector is $sel" >&2; exit 1; }

deadline=$((SECONDS + TIMEOUT))
while :; do
  eps=$($K -n $NS get endpoints inventory -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
  code=$($K -n $NS exec deploy/loadgen -- curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
         "http://order.${NS}.svc:8080/api/orders/check?item=a" 2>/dev/null || echo 000)
  if [[ -n "$eps" && "$code" == "200" ]]; then break; fi
  (( SECONDS > deadline )) && { echo "baseline not reached (endpoints='$eps' order=$code)" >&2; exit 1; }
  sleep 3
done
echo "baseline OK: inventory image=$img selector=app=$sel endpoints=[$eps] order=200"
