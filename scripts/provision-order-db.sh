#!/usr/bin/env bash
# KAN-96 §3: provision the dedicated database identity for reference service A (order), used only by
# deploy/overlays/rancher-desktop-chaos (ORDER_DB_ENABLED=true).
#   scripts/provision-order-db.sh
# Idempotent. On the shared PostgreSQL server in liveprobe-system it ensures:
#   - role `order_app` (`order` is reserved) LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
#     CONNECTION LIMIT 8 (pool 4 x 2 during a rollout surge), password = Secret liveprobe-target/order-db
#     (created by scripts/create-secrets.sh)
#   - database `order_app` owned by that role, PUBLIC privileges revoked; no tables (request path is SELECT 1)
#   - PUBLIC CONNECT revoked on the `liveprobe` and `inventory` databases
# It ends with an isolation self-check: order_app can open order_app but neither liveprobe nor inventory.
# The password travels Secret -> stdin of psql inside the postgres pod; it is never echoed or put on a
# command line. Only the target-namespace Secret and the postgres pod are touched.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:-rancher-desktop}"
K=(kubectl --context "$CTX")
SYS=liveprobe-system
PGPOD="${PGPOD:-postgres-0}"

"${K[@]}" -n liveprobe-target get secret order-db >/dev/null \
  || { echo "missing Secret liveprobe-target/order-db; run scripts/create-secrets.sh" >&2; exit 1; }
"${K[@]}" -n "$SYS" wait --for=condition=Ready "pod/$PGPOD" --timeout=180s >/dev/null

admin_psql() { "${K[@]}" -n "$SYS" exec -i "$PGPOD" -- psql -v ON_ERROR_STOP=1 -q -U liveprobe -d "$1"; }

# 1) role + password (psql variable read from stdin; \gexec keeps it out of argv/logs)
{
  printf '\\set pw '"'"
  "${K[@]}" -n liveprobe-target get secret order-db -o jsonpath='{.data.password}' | base64 -d | sed "s/'/''/g"
  printf "'"'\n'
  cat <<'SQL'
SELECT 'CREATE ROLE order_app LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'order_app') \gexec
ALTER ROLE order_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 8 PASSWORD :'pw';
\unset pw
SELECT 'CREATE DATABASE order_app OWNER order_app' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'order_app') \gexec
REVOKE ALL ON DATABASE order_app FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE liveprobe FROM PUBLIC;
SELECT 'REVOKE CONNECT, TEMPORARY ON DATABASE inventory FROM PUBLIC' WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname = 'inventory') \gexec
SQL
} | admin_psql postgres
echo "role order_app + database order_app ensured"

# 2) the credential works over TCP + scram for the request-path lookup (SELECT 1).
#    First stdin line = password (consumed by `read`), the rest is SQL for psql.
secret_pw() { "${K[@]}" -n liveprobe-target get secret order-db -o jsonpath='{.data.password}' | base64 -d; echo; }
as_order() { # db [psql args...]; stdin: password line then SQL
  local db="$1"; shift
  "${K[@]}" -n "$SYS" exec -i "$PGPOD" -- sh -c 'read -r pw; PGPASSWORD="$pw"; export PGPASSWORD; exec psql -v ON_ERROR_STOP=1 -q -h 127.0.0.1 -U order_app -d "$0" "$@"' "$db" "$@"
}
[[ "$(secret_pw | as_order order_app -Atc 'select 1')" == "1" ]] \
  || { echo "FAIL: role order_app cannot run SELECT 1 on database order_app" >&2; exit 1; }
echo "PASS: role order_app runs SELECT 1 on database order_app"

# 3) isolation self-check: order_app must not reach the LiveProbe or inventory databases
for db in liveprobe inventory; do
  if secret_pw | as_order "$db" -Atc 'select 1' >/dev/null 2>&1; then
    echo "FAIL: role order_app can connect to the $db database" >&2; exit 1
  fi
  echo "PASS: role order_app cannot connect to database $db"
done
