#!/usr/bin/env bash
# Positive and negative Kubernetes RBAC assertions for every LiveProbe ServiceAccount (KAN-44, extended in KAN-63).
# Uses `kubectl auth can-i --as=<sa>` through the operator's kubeconfig; exits non-zero on any mismatch.
#   scripts/rbac-matrix.sh
set -euo pipefail
CTX="${KUBE_CONTEXT:-rancher-desktop}"
SYS=liveprobe-system OBS=liveprobe-observability TGT=liveprobe-target
PASS=0
FAIL=0
check() { # expected sa(ns:name) verb resource namespace|-|-A
  local want="$1" sa="$2" verb="$3" resource="$4" ns="$5" got
  local args=(--context "$CTX" auth can-i "$verb" "$resource" "--as=system:serviceaccount:${sa}")
  # `pods/log` is parsed by kubectl as pod NAME "log", not the log subresource.
  # Always ask SubjectAccessReview for pods + --subresource=log.
  if [[ "$resource" == "pods/log" ]]; then
    args=(--context "$CTX" auth can-i "$verb" pods --subresource=log "--as=system:serviceaccount:${sa}")
  fi
  if [[ "$ns" == "-A" ]]; then args+=(--all-namespaces); elif [[ "$ns" != '-' ]]; then args+=(-n "$ns"); fi
  got="$(kubectl "${args[@]}" 2>/dev/null || true)"
  if [[ "$got" == "$want" ]]; then
    printf 'PASS %-3s %-36s %-9s %-45s %s\n' "$want" "$sa" "$verb" "$resource" "$ns"
    PASS=$((PASS + 1))
  else
    printf 'FAIL expected=%s actual=%s sa=%s verb=%s resource=%s ns=%s\n' "$want" "$got" "$sa" "$verb" "$resource" "$ns" >&2
    FAIL=$((FAIL + 1))
  fi
}

LP="$SYS:liveprobe"
echo "== LiveProbe API ServiceAccount ($LP)"
# positive: cluster-wide workload observation; no target writes or direct pod logs
check yes "$LP" list   pods                             -
check yes "$LP" watch  pods                             -A
check no  "$LP" get    pods/log                         $TGT
check no  "$LP" list   pods/log                         $TGT
check yes "$LP" list   events                           $TGT
check yes "$LP" get    deployments.apps                 $TGT
check yes "$LP" list   replicasets.apps                 $TGT
check yes "$LP" get    endpoints                        $TGT
check yes "$LP" list   endpointslices.discovery.k8s.io  $TGT
check yes "$LP" get    nodes                            -
check no  "$LP" patch  deployments.apps                 $TGT
check no  "$LP" update deployments.apps                 $TGT
check no  "$LP" patch  deployments.apps/order           $TGT
check no  "$LP" patch  deployments.apps/inventory       $TGT
# negative: Secret reads in every namespace
for ns in $TGT $SYS $OBS kube-system default; do
  check no "$LP" get  secrets $ns
  check no "$LP" list secrets $ns
done
check no "$LP" list  secrets -A
check no "$LP" watch secrets -A
# negative: any write outside the target namespace
for ns in $SYS $OBS kube-system default; do
  check no "$LP" patch  deployments.apps $ns
  check no "$LP" update deployments.apps $ns
  check no "$LP" create pods             $ns
  check no "$LP" delete pods             $ns
  check no "$LP" patch  configmaps       $ns
  check no "$LP" patch  services         $ns
done
check no "$LP" create namespaces -
check no "$LP" delete namespaces -
# negative: writes in the target namespace beyond Deployment patch/update
check no "$LP" create deployments.apps $TGT
check no "$LP" delete deployments.apps $TGT
check no "$LP" patch  services         $TGT
check no "$LP" delete pods             $TGT
check no "$LP" create pods/exec        $TGT
check no "$LP" create pods/eviction    $TGT
check no "$LP" patch  configmaps       $TGT
# negative: node changes, RBAC escalation, impersonation, token minting, wildcard
for verb in create update patch delete; do check no "$LP" $verb nodes -; done
check no "$LP" patch    nodes/status                                     -
check no "$LP" create   clusterroles.rbac.authorization.k8s.io          -
check no "$LP" create   clusterrolebindings.rbac.authorization.k8s.io   -
check no "$LP" create   rolebindings.rbac.authorization.k8s.io          $TGT
check no "$LP" escalate roles.rbac.authorization.k8s.io                 $TGT
check no "$LP" bind     roles.rbac.authorization.k8s.io                 $TGT
check no "$LP" impersonate serviceaccounts                               $SYS
check no "$LP" create   serviceaccounts/token                            $SYS
check no "$LP" '*'      '*'                                              -

echo "== rollback controller ServiceAccount (separate identity)"
RB="$SYS:rollback-controller"
check yes "$RB" get   deployments.apps/inventory $TGT
check yes "$RB" patch deployments.apps/inventory $TGT
check yes "$RB" patch deployments.apps/order $TGT
check no  "$RB" patch deployments.apps/loadgen $TGT
check no  "$RB" patch deployments.apps/inventory $SYS
check yes "$RB" list  replicasets.apps $TGT
check no  "$RB" patch services $TGT
check no  "$RB" get secrets $TGT
check no  "$RB" get pods/log $TGT
check no  "$RB" create serviceaccounts/token $TGT

echo "== observability ServiceAccounts"
for sa in "$OBS:prometheus" "$OBS:alloy" "$OBS:kube-state-metrics"; do
  check no "$sa" get    secrets $SYS
  check no "$sa" list   secrets -A
  check no "$sa" patch  deployments.apps $TGT
  check no "$sa" delete pods $TGT
  check no "$sa" patch  nodes -
  check no "$sa" create clusterrolebindings.rbac.authorization.k8s.io -
done
check yes "$OBS:prometheus"         list  pods             -A
check yes "$OBS:prometheus"         get   nodes            -
check yes "$OBS:alloy"              get   pods/log         $TGT
check yes "$OBS:kube-state-metrics" watch deployments.apps -A
check no  "$OBS:kube-state-metrics" list  configmaps       -A

echo "== default ServiceAccounts (workloads without API access; token automount disabled)"
for ns in $SYS $OBS $TGT; do
  check no "$ns:default" list  pods             $ns
  check no "$ns:default" get   secrets          $ns
  check no "$ns:default" patch deployments.apps $TGT
done

printf 'RBAC: %s passed, %s failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
