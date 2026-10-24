#!/usr/bin/env bash
# KAN-47: provision the dedicated database identity for reference service B (inventory).
#   scripts/provision-inventory-db.sh
# Idempotent. On the shared PostgreSQL server in liveprobe-system it ensures:
#   - role `inventory` (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, CONNECTION LIMIT 12) whose password is
#     the value of Secret liveprobe-target/inventory-db (created by scripts/create-secrets.sh)
#   - database `inventory` owned by that role, PUBLIC CONNECT revoked
#   - PUBLIC CONNECT revoked on the LiveProbe database, so the inventory role cannot open it
#   - schema/fixture from services/inventory/db/schema.sql applied AS the inventory role
# The password travels Secret -> stdin of psql inside the postgres pod; it is never echoed or put on a
# command line. Only the target-namespace Secret and the postgres pod are touched.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:-rancher-desktop}"
K=(kubectl --context "$CTX")
SYS=liveprobe-system
PGPOD="${PGPOD:-postgres-0}"

"${K[@]}" -n liveprobe-target get secret inventory-db >/dev/null \
  || { echo "missing Secret liveprobe-target/inventory-db; run scripts/create-secrets.sh" >&2; exit 1; }
"${K[@]}" -n "$SYS" wait --for=condition=Ready "pod/$PGPOD" --timeout=180s >/dev/null

admin_psql() { "${K[@]}" -n "$SYS" exec -i "$PGPOD" -- psql -v ON_ERROR_STOP=1 -q -U liveprobe -d "$1"; }

# 1) role + password (psql variable read from stdin; \gexec keeps it out of argv/logs)
{
  printf '\\set pw '"'"
  "${K[@]}" -n liveprobe-target get secret inventory-db -o jsonpath='{.data.password}' | base64 -d | sed "s/'/''/g"
  printf "'"'\n'
  cat <<'SQL'
SELECT 'CREATE ROLE inventory LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inventory') \gexec
ALTER ROLE inventory WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 12 PASSWORD :'pw';
\unset pw
SELECT 'CREATE DATABASE inventory OWNER inventory' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'inventory') \gexec
REVOKE ALL ON DATABASE inventory FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE liveprobe FROM PUBLIC;
SQL
} | admin_psql postgres
echo "role inventory + database inventory ensured"

# 2) schema + fixture as the inventory role itself (proves the credential works over TCP + scram).
#    First stdin line = password (consumed by `read`), the rest is SQL for psql.
secret_pw() { "${K[@]}" -n liveprobe-target get secret inventory-db -o jsonpath='{.data.password}' | base64 -d; echo; }
as_inventory() { # db [psql args...]; stdin: password line then SQL
  local db="$1"; shift
  "${K[@]}" -n "$SYS" exec -i "$PGPOD" -- sh -c 'read -r pw; PGPASSWORD="$pw"; export PGPASSWORD; exec psql -v ON_ERROR_STOP=1 -q -h 127.0.0.1 -U inventory -d "$0" "$@"' "$db" "$@"
}
{ secret_pw; cat services/inventory/db/schema.sql; } | as_inventory inventory
echo "schema inventory.stock + baseline fixture applied as role inventory"

# 3) isolation self-check: inventory must not reach the LiveProbe database
if secret_pw | as_inventory liveprobe -Atc 'select 1' >/dev/null 2>&1; then
  echo "FAIL: role inventory can connect to the liveprobe database" >&2; exit 1
fi
echo "PASS: role inventory cannot connect to database liveprobe"
