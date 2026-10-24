#!/usr/bin/env bash
# Remove everything LiveProbe installed: its three namespaces (including PVCs, i.e. the PostgreSQL,
# Prometheus and Loki data, and Secrets) and its cluster-scoped RBAC. Nothing outside those objects is touched.
#
#   scripts/uninstall.sh --yes                 # backup DB (scripts/pg-backup.sh) then delete
#   scripts/uninstall.sh --dry-run             # server-side dry run: list exactly what would be deleted
#   NO_BACKUP=1 scripts/uninstall.sh --yes     # skip the pre-delete backup (data is lost)
#
# The object list comes from the rendered overlay, so it tracks the manifests. Deleting a namespace that
# is not labelled app.kubernetes.io/part-of=liveprobe* is refused.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
OVERLAY="${OVERLAY:-deploy/overlays/rancher-desktop}"
K="kubectl --context ${CTX}"

mode=""
case "${1:-}" in
  --yes) mode=delete ;;
  --dry-run) mode=dry ;;
  *) echo "usage: $0 --yes | --dry-run   (context: ${CTX})" >&2; exit 2 ;;
esac

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
kubectl kustomize "$OVERLAY" > "$rendered"

# name lists from the render: cluster-scoped RBAC and namespaces
read -r -a cluster_rbac <<< "$(python3 scripts/render-names.py "$rendered" ClusterRole ClusterRoleBinding)"
read -r -a namespaces   <<< "$(python3 scripts/render-names.py "$rendered" Namespace)"

for ns in "${namespaces[@]}"; do
  part_of="$($K get ns "$ns" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/part-of}' 2>/dev/null || true)"
  if [[ -n "$part_of" && "$part_of" != liveprobe* ]]; then
    echo "REFUSE: namespace $ns exists but is not owned by LiveProbe (part-of=$part_of)" >&2; exit 1
  fi
done

echo "==> context ${CTX}; namespaces: ${namespaces[*]}"
echo "==> cluster-scoped: ${cluster_rbac[*]}"

if [[ "$mode" == dry ]]; then
  for ns in "${namespaces[@]}"; do
    $K get ns "$ns" >/dev/null 2>&1 || { echo "(absent) namespace/$ns"; continue; }
    echo "--- namespace/$ns contents that would be deleted:"
    $K -n "$ns" get all,pvc,configmap,secret,serviceaccount,role,rolebinding,networkpolicy,ingress -o name 2>/dev/null | sed 's/^/    /'
  done
  $K delete clusterrole,clusterrolebinding "${cluster_rbac[@]}" --ignore-not-found --dry-run=server
  $K delete ns "${namespaces[@]}" --ignore-not-found --dry-run=server
  echo "dry run only; nothing deleted"
  exit 0
fi

if [[ "${NO_BACKUP:-0}" != 1 ]] && $K -n liveprobe-system get pod postgres-0 >/dev/null 2>&1; then
  echo "==> pre-delete database backup"
  KUBE_CONTEXT="$CTX" scripts/pg-backup.sh
fi

$K delete clusterrolebinding,clusterrole "${cluster_rbac[@]}" --ignore-not-found
$K delete ns "${namespaces[@]}" --ignore-not-found --wait=true --timeout=300s
echo "uninstalled"
