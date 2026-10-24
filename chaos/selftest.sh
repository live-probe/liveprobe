#!/usr/bin/env bash
# KAN-37 verification: inject + observe + recover each family N times (LiveProbe not involved).
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
K="kubectl --context ${CTX} -n liveprobe-target"
N="${N:-2}"

order_code() { $K exec deploy/loadgen -- curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  "http://order.liveprobe-target.svc:8080/api/orders/check?item=a" 2>/dev/null || echo 000; }

wait_for() { # desc timeout cmd...
  local desc="$1" t="$2"; shift 2; local end=$((SECONDS + t))
  until "$@"; do (( SECONDS > end )) && { echo "TIMEOUT waiting for $desc"; return 1; }; sleep 3; done
  echo "  observed: $desc"
}
pull_failing() { $K get pods -l app=inventory -o jsonpath='{range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{"\n"}{end}' | grep -qE 'ImagePullBackOff|ErrImagePull'; }
no_endpoints() { [[ -z "$($K get endpoints inventory -o jsonpath='{.subsets[*].addresses[*].ip}')" ]]; }
order_5xx() { [[ "$(order_code)" =~ ^5 ]]; }

for i in $(seq 1 "$N"); do
  echo "== family1 run $i"
  chaos/inject.sh family1
  wait_for "inventory pod ImagePullBackOff/ErrImagePull" 180 pull_failing
  echo "  order status during family1: $(order_code) (old pod keeps serving)"
  chaos/recover.sh
  echo "== family4 run $i"
  chaos/inject.sh family4
  wait_for "inventory endpoints = 0" 60 no_endpoints
  # established keep-alive connections from order may survive the endpoint removal for a while
  wait_for "order returns 5xx" 180 order_5xx
  echo "  order status: $(order_code)"
  chaos/recover.sh
done
echo "chaos kit verification passed ($N runs per family)"
