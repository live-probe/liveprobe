#!/usr/bin/env bash
# Static validation of the Kustomize tree (KAN-61):
#   1) render base and every overlay (default: rancher-desktop + portable-example)
#   2) kubeconform -strict on every overlay render
#   3) base portability: no K3s/Traefik/Helm-controller kinds, no environment values in base
#   4) each overlay render carries exactly its own platform.yaml values (ingress class, storage class,
#      hosts, ingress-controller selector, API CIDR) and registry-rewritten images; local-config not emitted
set -euo pipefail
cd "$(dirname "$0")/.."
OVERLAYS=(${OVERLAYS:-deploy/overlays/rancher-desktop deploy/overlays/portable-example})
KCF_IMAGE="${KCF_IMAGE:-ghcr.io/yannh/kubeconform:v0.7.0}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

kubectl kustomize deploy/base > "$tmp/base.yaml"
echo "rendered $(grep -c '^kind:' "$tmp/base.yaml") objects from deploy/base"

if grep -rnE 'apiVersion: *(traefik\.io|traefik\.containo\.us|k3s\.cattle\.io|helm\.cattle\.io)' deploy/base; then
  echo "FAIL: platform-specific kinds found in deploy/base" >&2; exit 1
fi
if grep -nE 'ingressClassName|storageClassName|\.localhost|local-path|traefik' "$tmp/base.yaml"; then
  echo "FAIL: environment-specific values found in rendered base" >&2; exit 1
fi
echo "base portability check: no traefik.io / k3s.cattle.io / helm.cattle.io kinds, no class/host/traefik values"

i=0
for ov in "${OVERLAYS[@]}"; do
  out="$tmp/overlay-$i.yaml"; i=$((i + 1))
  kubectl kustomize "$ov" > "$out"
  echo "== $ov: rendered $(grep -c '^kind:' "$out") objects"
  docker run --rm -i "$KCF_IMAGE" -strict -summary -kubernetes-version "${K8S_VERSION:-1.33.0}" - < "$out"
  python3 scripts/check-overlay.py "$ov" "$out"
done
