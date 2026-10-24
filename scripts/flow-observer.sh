#!/usr/bin/env bash
# Install/uninstall/inspect the network-flow collector (Retina + Hubble Relay) in kube-system.
#   scripts/flow-observer.sh install|uninstall|status
# Adds only the `retina` Helm release in kube-system. Never touches the CNI.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
VERSION="${RETINA_VERSION:-v1.2.9}"      # pinned; arm64 image verified
CHART=oci://ghcr.io/microsoft/retina/charts/retina-hubble
case "${1:-status}" in
  install)
    helm --kube-context "$CTX" upgrade --install retina "$CHART" --version "$VERSION" -n kube-system \
      -f deploy/optional/flow-observer/values.yaml \
      --set operator.tag="$VERSION" --set agent.tag="$VERSION" --set agent.init.tag="$VERSION" \
      --wait --timeout 6m ;;
  uninstall) helm --kube-context "$CTX" -n kube-system uninstall retina ;;
  status)
    helm --kube-context "$CTX" -n kube-system status retina | head -6
    kubectl --context "$CTX" -n kube-system get pods -l 'app.kubernetes.io/instance=retina' ;;
  *) echo "usage: $0 install|uninstall|status" >&2; exit 2 ;;
esac
