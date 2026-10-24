#!/usr/bin/env bash
# Chaos kit — fault injection for the LiveProbe target namespace.
# Uses the OPERATOR's kubeconfig (never the LiveProbe ServiceAccount).
#
#   chaos/inject.sh family1   # bad image tag on inventory  -> ImagePullBackOff
#   chaos/inject.sh family4   # break inventory Service selector -> 0 endpoints, order 5xx
set -euo pipefail
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS=liveprobe-target
K="kubectl --context ${CTX} -n ${NS}"

case "${1:-}" in
  family1)
    # Never reuse an old failed ReplicaSet (and its stale events) in a new E2E run.
    token=$(uuidgen | tr '[:upper:]' '[:lower:]' | tr -d '-')
    image="liveprobe/inventory:does-not-exist-${token}"
    $K set image deploy/inventory "inventory=${image}"
    echo "injected family1: deploy/inventory image -> ${image}"
    ;;
  family4)
    $K patch service inventory --type=merge -p '{"spec":{"selector":{"app":"inventory-broken"}}}'
    echo "injected family4: svc/inventory selector -> app=inventory-broken"
    ;;
  *)
    echo "usage: $0 family1|family4" >&2; exit 2 ;;
esac
