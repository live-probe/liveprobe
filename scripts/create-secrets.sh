#!/usr/bin/env bash
# Create (idempotently) the Secrets LiveProbe needs.
# Secret values are generated locally or read from files and are NEVER printed.
#
#   scripts/create-secrets.sh            # create missing secrets only
#   scripts/create-secrets.sh --force    # recreate LLM credential secret (e.g. after re-login)
#
# LLM provider credentials:
#   - codex_cli : copies ~/.codex/auth.json (override with CODEX_AUTH_FILE) into Secret liveprobe-llm/auth.json
#   - openai_api: OPENAI_API_KEY_FILE=<private file> -> liveprobe-llm/OPENAI_API_KEY
#   - claude_cli: CLAUDE_CODE_OAUTH_TOKEN_FILE=<private file> -> liveprobe-llm/CLAUDE_CODE_OAUTH_TOKEN
#   - anthropic_api: ANTHROPIC_API_KEY_FILE=<private file> -> liveprobe-llm/ANTHROPIC_API_KEY
set -euo pipefail

CTX="${KUBE_CONTEXT:-rancher-desktop}"
K="kubectl --context ${CTX}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

SYS=liveprobe-system
OBS=liveprobe-observability

rand() { openssl rand -hex "${1:-24}"; }

exists() { $K -n "$1" get secret "$2" >/dev/null 2>&1; }

for ns in "$SYS" "$OBS" liveprobe-target; do
  $K get ns "$ns" >/dev/null 2>&1 || $K create ns "$ns" >/dev/null
done

tmpdir="$(mktemp -d)"
chmod 700 "$tmpdir"
trap 'rm -rf "$tmpdir"' EXIT

# helper: create secret from literal files in tmpdir (keeps values off the command line)
mk_secret() { # ns name key=value-file...
  local ns="$1" name="$2"; shift 2
  local args=()
  for kv in "$@"; do args+=("--from-file=${kv}"); done
  $K -n "$ns" create secret generic "$name" "${args[@]}" >/dev/null
  echo "created secret ${ns}/${name}"
}

if ! exists "$SYS" postgres-credentials; then
  rand 24 | tr -d '\n' > "$tmpdir/pg"
  mk_secret "$SYS" postgres-credentials "password=$tmpdir/pg"
else echo "exists  secret ${SYS}/postgres-credentials"; fi

# No admin/user secret: the dashboard has no login (local-only PoC; see liveprobe/app/auth.py).
# Shared bearer token for Alertmanager -> LiveProbe webhook (same value in both namespaces)
if ! exists "$SYS" liveprobe-webhook || ! exists "$OBS" alertmanager-webhook; then
  rand 32 | tr -d '\n' > "$tmpdir/wh"
  $K -n "$SYS" delete secret liveprobe-webhook --ignore-not-found >/dev/null
  $K -n "$OBS" delete secret alertmanager-webhook --ignore-not-found >/dev/null
  mk_secret "$SYS" liveprobe-webhook "token=$tmpdir/wh"
  mk_secret "$OBS" alertmanager-webhook "token=$tmpdir/wh"
else echo "exists  secret ${SYS}/liveprobe-webhook + ${OBS}/alertmanager-webhook"; fi

# KAN-47: dedicated credential for reference service B (inventory) -> its own `inventory` database/role.
# Never shares the LiveProbe DB credential. scripts/provision-inventory-db.sh applies it to the role.
if ! exists liveprobe-target inventory-db; then
  rand 24 | tr -d '\n' > "$tmpdir/inv"
  mk_secret liveprobe-target inventory-db "password=$tmpdir/inv"
else echo "exists  secret liveprobe-target/inventory-db"; fi

# KAN-68: control-plane-only operator credential, never shared with LiveProbe or Ingress.
if ! exists liveprobe-target inventory-chaos-operator; then
  rand 32 | tr -d '\n' > "$tmpdir/chaos"
  mk_secret liveprobe-target inventory-chaos-operator "token=$tmpdir/chaos"
else echo "exists  secret liveprobe-target/inventory-chaos-operator"; fi

# KAN-96: order's chaos operator credential (independent of inventory's). Referenced only by
# deploy/overlays/rancher-desktop-chaos; never shared with LiveProbe or Ingress.
if ! exists liveprobe-target order-chaos-operator; then
  rand 32 | tr -d '\n' > "$tmpdir/ochaos"
  mk_secret liveprobe-target order-chaos-operator "token=$tmpdir/ochaos"
else echo "exists  secret liveprobe-target/order-chaos-operator"; fi

# KAN-96 §3: dedicated credential for order -> its own `order_app` database/role (chaos overlay only).
# scripts/provision-order-db.sh applies it to the role.
if ! exists liveprobe-target order-db; then
  rand 24 | tr -d '\n' > "$tmpdir/odb"
  mk_secret liveprobe-target order-db "password=$tmpdir/odb"
else echo "exists  secret liveprobe-target/order-db"; fi

if ! exists "$OBS" grafana-admin; then
  printf 'admin' > "$tmpdir/gu"
  rand 12 | tr -d '\n' > "$tmpdir/gp"
  mk_secret "$OBS" grafana-admin "admin-user=$tmpdir/gu" "admin-password=$tmpdir/gp"
  echo "  grafana password: kubectl --context ${CTX} -n ${OBS} get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
else echo "exists  secret ${OBS}/grafana-admin"; fi

# LLM credentials
AUTH_FILE="${CODEX_AUTH_FILE:-$HOME/.codex/auth.json}"
if [[ $FORCE -eq 1 ]] || ! exists "$SYS" liveprobe-llm; then
  args=()
  if [[ -f "$AUTH_FILE" ]]; then
    args+=("--from-file=auth.json=${AUTH_FILE}")
  fi
  # API keys and Claude OAuth token are accepted only as private files, never env/argv/chat.
  for key in OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; do
    var="${key}_FILE"; file="${!var:-}"
    [[ -z "$file" ]] && continue
    [[ -s "$file" ]] || { echo "FAIL: ${var} is empty or missing" >&2; exit 1; }
    tr -d '\r\n' < "$file" > "$tmpdir/$key"
    [[ -s "$tmpdir/$key" ]] || { echo "FAIL: ${var} contains no credential" >&2; exit 1; }
    args+=("--from-file=${key}=$tmpdir/$key")
  done
  if [[ $FORCE -eq 1 ]] && exists "$SYS" liveprobe-llm; then
    # Never silently discard another provider's key during a partial rotation.
    # Inspect key NAMES only; never read or print Secret values.
    existing_keys=" $($K -n "$SYS" get secret liveprobe-llm -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}') "
    for key in $existing_keys; do
      found=0
      for arg in "${args[@]}"; do [[ "$arg" == "--from-file=$key="* ]] && found=1; done
      [[ $found -eq 1 ]] || { echo "FAIL: --force would remove existing ${key}; supply its source file" >&2; exit 1; }
    done
  fi
  if [[ ${#args[@]} -eq 0 ]]; then
    echo "WARN: no LLM credentials (auth.json or *_FILE); creating empty liveprobe-llm secret (LLM calls will fail -> incidents hand off)" >&2
  fi
  $K -n "$SYS" delete secret liveprobe-llm --ignore-not-found >/dev/null
  $K -n "$SYS" create secret generic liveprobe-llm ${args[@]+"${args[@]}"} >/dev/null
  $K -n "$SYS" label secret liveprobe-llm app.kubernetes.io/part-of=liveprobe --overwrite >/dev/null
  echo "created secret ${SYS}/liveprobe-llm (keys: $($K -n "$SYS" get secret liveprobe-llm -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}'))"
  if [[ $FORCE -eq 1 ]]; then
    # PVC copy of CODEX_HOME is authoritative after first start; force re-seed on next start.
    echo "  note: --force also requires resetting the codex PVC copy; the API pod re-seeds when /var/lib/liveprobe/codex/.seeded is absent."
  fi
else echo "exists  secret ${SYS}/liveprobe-llm"; fi

# Spare Codex account (optional). The operator logs the second account in OUTSIDE this script:
#   CODEX_HOME=$HOME/.codex-b codex login
# then:  CODEX_SPARE_AUTH_FILE=$HOME/.codex-b/auth.json scripts/create-secrets.sh
# Only the file path is used; the credential is never printed, logged or placed in argv.
SPARE_FILE="${CODEX_SPARE_AUTH_FILE:-}"
if [[ -n "$SPARE_FILE" ]]; then
  [[ -s "$SPARE_FILE" ]] || { echo "FAIL: CODEX_SPARE_AUTH_FILE is empty or missing" >&2; exit 1; }
  if [[ -n "${AUTH_FILE:-}" && -f "$AUTH_FILE" ]] && cmp -s "$SPARE_FILE" "$AUTH_FILE"; then
    echo "FAIL: spare auth file is identical to the primary account's; log in with a different account" >&2; exit 1
  fi
  $K -n "$SYS" delete secret liveprobe-llm-spare --ignore-not-found >/dev/null
  $K -n "$SYS" create secret generic liveprobe-llm-spare "--from-file=auth.json=${SPARE_FILE}" >/dev/null
  $K -n "$SYS" label secret liveprobe-llm-spare app.kubernetes.io/part-of=liveprobe --overwrite >/dev/null
  echo "created secret ${SYS}/liveprobe-llm-spare (spare Codex account); restart liveprobe-api to seed its home"
elif exists "$SYS" liveprobe-llm-spare; then echo "exists  secret ${SYS}/liveprobe-llm-spare"
else echo "note    no spare Codex account configured (single-account mode)"; fi
