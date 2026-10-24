#!/usr/bin/env bash
# KAN-69: build both Spring target services as a multi-platform OCI artifact (no registry push).
#   scripts/build-target-multiarch.sh [output-directory]
# Requires a buildx builder that supports linux/amd64 + linux/arm64 and the OCI exporter.
# The result is two OCI image-layout tarballs, each with an index containing BOTH architectures.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-.e2e-tmp/KAN-47-multiarch}"
mkdir -p "$OUT"
for app in order inventory; do
  printf '==> %s: linux/amd64,linux/arm64\n' "$app"
  docker buildx build --platform linux/amd64,linux/arm64 \
    --tag "liveprobe/$app:multiarch" \
    --output "type=oci,dest=$OUT/$app.oci.tar" "services/$app"
  python3 scripts/inspect-target-oci.py "$OUT/$app.oci.tar" "$app"
done
