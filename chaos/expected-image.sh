#!/usr/bin/env bash
# Print the image the rancher-desktop overlay renders for a liveprobe-target Deployment's first
# container (KAN-118: digest-pinned, e.g. liveprobe/inventory@sha256:...). recover.sh and
# e2e-mvp0.sh compare against this so the baseline follows the declarative pin, not a hard-coded tag.
#   chaos/expected-image.sh inventory|order
set -euo pipefail
cd "$(dirname "$0")/.."
name="${1:?usage: $0 inventory|order}"
img=$(kubectl kustomize "${OVERLAY:-deploy/overlays/rancher-desktop}" | awk -v want="$name" '
  function flush() { if (kind == "Deployment" && nm == want && ns == "liveprobe-target" && image != "") { print image; found = 1; exit } }
  /^---/ { flush(); kind = nm = ns = image = ""; inmeta = 0; next }
  /^kind: / { kind = $2 }
  /^metadata:/ { inmeta = 1; next }
  /^[^ ]/ { inmeta = 0 }
  inmeta && /^  name: / { nm = $2 }
  inmeta && /^  namespace: / { ns = $2 }
  image == "" && /^ +(- )?image: / { sub(/^ +(- )?image: */, ""); image = $0 }
  END { if (!found) flush() }
')
[[ -n "$img" ]] || { echo "no liveprobe-target Deployment '$name' image in overlay" >&2; exit 1; }
printf '%s\n' "$img"
