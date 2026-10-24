#!/usr/bin/env bash
# Regenerate liveprobe/app/hubble_gen from the cilium/cilium Hubble protobuf API (Apache-2.0).
#   scripts/gen-hubble-stubs.sh [git-ref]      # default: main
set -euo pipefail
cd "$(dirname "$0")/.."
REF="${1:-main}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
git clone -q --depth 1 --branch "$REF" https://github.com/cilium/cilium.git "$work/cilium"
out=liveprobe/app/hubble_gen
rm -rf "$out"; mkdir -p "$out"
(cd liveprobe && uv run python -m grpc_tools.protoc -I "$work/cilium/api/v1" -I "$work/cilium/vendor" \
  --python_out="app/hubble_gen" --grpc_python_out="app/hubble_gen" \
  "$work/cilium/api/v1/flow/flow.proto" "$work/cilium/api/v1/observer/observer.proto" "$work/cilium/api/v1/relay/relay.proto")
python3 - "$out" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
for f in root.rglob("*_pb2*.py"):
    t = f.read_text()
    t = re.sub(r"^from (flow|relay|observer)\.(\w+) import", r"from app.hubble_gen.\1.\2 import", t, flags=re.M)
    t = re.sub(r"^from (flow|relay|observer) import", r"from app.hubble_gen.\1 import", t, flags=re.M)
    f.write_text(t)
for d in [root, *[p for p in root.iterdir() if p.is_dir()]]:
    (d / "__init__.py").touch()
(root / "__init__.py").write_text('"""Generated Hubble gRPC stubs (cilium/cilium api/v1). Regenerate with scripts/gen-hubble-stubs.sh."""\n')
PY
echo "generated $out from cilium@$REF"
