#!/usr/bin/env bash
# KAN-67 verification for the target services (order = A, inventory = B) with real Maven + PostgreSQL.
#   scripts/verify-target-services.sh
# - order:     mvn test (unit tests)
# - inventory: mvn verify = unit tests + *IT integration tests against a throwaway pgvector/PostgreSQL
#              container provisioned with services/inventory/db/schema.sql as a dedicated `inventory` role.
# Nothing is pushed and no cluster is touched. The throwaway DB password is generated per run, kept in
# a 0600 temp file and never printed. The containers and network are removed on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

MVN_IMAGE="${MVN_IMAGE:-maven:3.9-eclipse-temurin-21}"
PG_IMAGE="${PG_IMAGE:-pgvector/pgvector:0.8.6-pg17-bookworm}"
RUN_ID="kan67-$$"
NET="$RUN_ID-net"
PG="$RUN_ID-pg"
M2_VOLUME="${M2_VOLUME:-liveprobe-m2}"

tmp="$(mktemp -d)"; chmod 700 "$tmp"
cleanup() {
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

openssl rand -hex 16 | tr -d '\n' > "$tmp/pw"
openssl rand -hex 16 | tr -d '\n' > "$tmp/admin"
{ printf 'INVENTORY_DB_URL=jdbc:postgresql://%s:5432/inventory\n' "$PG"
  printf 'INVENTORY_DB_USER=inventory\n'
  printf 'INVENTORY_DB_PASSWORD=%s\n' "$(cat "$tmp/pw")"; } > "$tmp/env"
chmod 600 "$tmp"/*

docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=admin -e POSTGRES_DB=postgres -e POSTGRES_PASSWORD_FILE=/run/pgadmin \
  -v "$tmp/admin:/run/pgadmin:ro" "$PG_IMAGE" >/dev/null
for _ in $(seq 60); do
  docker exec "$PG" pg_isready -U admin -d postgres >/dev/null 2>&1 && break; sleep 1
done
sleep 2
# Same provisioning path as the cluster (scripts/provision-inventory-db.sh uses these SQL steps).
docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -q -U admin -d postgres \
  -v pw="$(cat "$tmp/pw")" <<'SQL'
CREATE ROLE inventory LOGIN PASSWORD :'pw' CONNECTION LIMIT 12;
CREATE DATABASE inventory OWNER inventory;
REVOKE ALL ON DATABASE inventory FROM PUBLIC;
SQL
docker exec -i -e PGPASSWORD="$(cat "$tmp/pw")" "$PG" psql -v ON_ERROR_STOP=1 -q -h 127.0.0.1 -U inventory -d inventory \
  < services/inventory/db/schema.sql
echo "==> throwaway PostgreSQL ready (dedicated role inventory, db inventory)"

mvn_run() { # dir goal [envfile]
  local dir="$1" goal="$2" envargs=()
  [[ -n "${3:-}" ]] && envargs=(--env-file "$3")
  docker run --rm --network "$NET" ${envargs[@]+"${envargs[@]}"} \
    -v "$PWD/$dir:/src" -v "$M2_VOLUME:/root/.m2" -w /src "$MVN_IMAGE" \
    mvn -B -q -Dstyle.color=never "$goal" \
    && echo "PASS mvn $goal $dir"
}
# Maven writes services/*/target/ (git-ignored build output).
mvn_run services/order test
mvn_run services/inventory verify "$tmp/env"
for d in services/order services/inventory; do
  for r in "$d"/target/surefire-reports "$d"/target/failsafe-reports; do
    [[ -d "$r" ]] && grep -h -o 'tests="[0-9]*" errors="[0-9]*" skipped="[0-9]*" failures="[0-9]*"' "$r"/TEST-*.xml \
      | awk -v d="$r" '{print d": "$0}'  || true
  done
done
