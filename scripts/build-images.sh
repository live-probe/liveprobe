#!/usr/bin/env bash
# Build local images (native arch) into the Rancher Desktop docker image store.
# The K3s kubelet uses the same docker runtime, so :dev images are visible without a registry.
#   scripts/build-images.sh [order inventory liveprobe dashboard]
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${TAG:-dev}"
targets=("$@")
[[ ${#targets[@]} -eq 0 ]] && targets=(order inventory liveprobe dashboard)

for t in "${targets[@]}"; do
  case "$t" in
    order|inventory) ctx="services/$t" ;;
    liveprobe)       ctx="liveprobe" ;;
    dashboard)       ctx="dashboard" ;;
    *) echo "unknown target $t" >&2; exit 2 ;;
  esac
  if [[ ! -f "$ctx/Dockerfile" ]]; then
    echo "skip $t (no $ctx/Dockerfile yet)"; continue
  fi
  echo "==> build liveprobe/$t:$TAG"
  docker build -t "liveprobe/$t:$TAG" "$ctx"
done
