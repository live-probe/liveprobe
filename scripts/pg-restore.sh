#!/usr/bin/env bash
# Restore a scripts/pg-backup.sh dump into an ISOLATED database and verify it against the manifest.
# This script never writes to the live LiveProbe database.
#
#   scripts/pg-restore.sh backups/liveprobe-<ts>.dump                      # ephemeral local docker pgvector (default)
#   RESTORE_MODE=cluster-scratch scripts/pg-restore.sh backups/<file>.dump # scratch DB on the in-cluster server
#
# Modes
#   docker          : starts a throwaway container from the same pgvector image, restores, verifies, removes it.
#   cluster-scratch : CREATE DATABASE liveprobe_restore_check_<ts> on postgres-0, restores, verifies, drops only
#                     that scratch database (name must carry the liveprobe_restore_check_ prefix). Uses space on
#                     the live PVC while it runs.
#
# Checks: sha256 matches, pg_restore exits 0, extension versions, Alembic revision, exact per-table row counts,
# non-null vector counts and a pgvector distance query on the restored data.
# Recovering the LIVE database is a manual, operator-authorized procedure: see docs/operations/kan45-deploy-data-security.md (section 2).
set -euo pipefail
cd "$(dirname "$0")/.."

dump="${1:?usage: $0 <backup.dump>}"
manifest="${dump}.manifest.json"
[[ -f "$dump" && -f "$manifest" ]] || { echo "FAIL: need $dump and $manifest" >&2; exit 2; }

MODE="${RESTORE_MODE:-docker}"
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS="${PG_NAMESPACE:-liveprobe-system}"
POD="${PG_POD:-postgres-0}"
LIVE_DB="${PG_DB:-liveprobe}"
DBUSER="${PG_USER:-liveprobe}"
PG_IMAGE="${PG_IMAGE:-pgvector/pgvector:0.8.6-pg17-bookworm}"
ts="$(date -u +%Y%m%dT%H%M%SZ | tr 'A-Z' 'a-z')"

want_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$manifest")"
got_sha="$(shasum -a 256 "$dump" | awk '{print $1}')"
[[ "$want_sha" == "$got_sha" ]] || { echo "FAIL: sha256 mismatch" >&2; exit 1; }
echo "==> sha256 OK"

case "$MODE" in
  docker)
    name="liveprobe-restore-check-${ts}"
    TARGET_DB=liveprobe
    cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    # Throwaway container: no published port, trust auth only inside the container, removed on exit.
    docker run -d --name "$name" --network none -e POSTGRES_HOST_AUTH_METHOD=trust \
      -e POSTGRES_USER="$DBUSER" -e POSTGRES_DB="$TARGET_DB" "$PG_IMAGE" >/dev/null
    for _ in $(seq 1 60); do
      docker exec "$name" pg_isready -U "$DBUSER" -d "$TARGET_DB" >/dev/null 2>&1 && break; sleep 1
    done
    # The image's entrypoint restarts postgres once after init; wait until it answers a query.
    for _ in $(seq 1 30); do
      docker exec "$name" psql -U "$DBUSER" -d "$TARGET_DB" -Atc 'select 1' >/dev/null 2>&1 && break; sleep 1
    done
    run_restore() { docker exec -i "$name" pg_restore -U "$DBUSER" -d "$TARGET_DB" --no-owner --no-privileges --exit-on-error; }
    run_psql() { docker exec -i "$name" psql -U "$DBUSER" -d "$TARGET_DB" -v ON_ERROR_STOP=1 -At; }
    echo "==> target: ephemeral docker container ${name} (network none)"
    ;;
  cluster-scratch)
    TARGET_DB="liveprobe_restore_check_${ts}"
    [[ "$TARGET_DB" == liveprobe_restore_check_* && "$TARGET_DB" != "$LIVE_DB" ]] || { echo "FAIL: refusing target db" >&2; exit 1; }
    K=(kubectl --context "$CTX" -n "$NS")
    admin() { "${K[@]}" exec -i "$POD" -- psql -U "$DBUSER" -d postgres -v ON_ERROR_STOP=1 -At; }
    cleanup() { printf 'DROP DATABASE IF EXISTS "%s" WITH (FORCE);\n' "$TARGET_DB" | admin >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    printf 'CREATE DATABASE "%s";\n' "$TARGET_DB" | admin >/dev/null
    run_restore() { "${K[@]}" exec -i "$POD" -- pg_restore -U "$DBUSER" -d "$TARGET_DB" --no-owner --no-privileges --exit-on-error; }
    run_psql() { "${K[@]}" exec -i "$POD" -- psql -U "$DBUSER" -d "$TARGET_DB" -v ON_ERROR_STOP=1 -At; }
    echo "==> target: scratch database ${TARGET_DB} on ${CTX}/${NS}/${POD} (live db ${LIVE_DB} untouched)"
    ;;
  *) echo "unknown RESTORE_MODE=$MODE" >&2; exit 2 ;;
esac

echo "==> pg_restore"
run_restore < "$dump"

echo "==> verify"
observed="$(run_psql <<'SQL'
SELECT 'ext|'||extname||'|'||extversion FROM pg_extension ORDER BY extname;
SELECT 'alembic|'||version_num FROM alembic_version;
SELECT format('select ''table|%s|''||count(*) from %I.%I;', c.relname, n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind='r' AND n.nspname='public' ORDER BY c.relname \gexec
SELECT format('select ''vectors|%s.%s|''||count(%I) from %I.%I;', c.relname, a.attname, a.attname, n.nspname, c.relname)
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_type t ON t.oid=a.atttypid
 WHERE t.typname='vector' AND n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1 \gexec
SELECT format('select ''knn|%s.%s|''||count(*) from (select 1 from %I.%I where %I is not null order by %I <=> (select %I from %I.%I where %I is not null limit 1) limit 3) s;',
              c.relname, a.attname, n.nspname, c.relname, a.attname, a.attname, a.attname, n.nspname, c.relname, a.attname)
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_type t ON t.oid=a.atttypid
 WHERE t.typname='vector' AND n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1 \gexec
SQL
)"

python3 - "$manifest" "$observed" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
ext, tables, vectors, knn, alembic = {}, {}, {}, {}, None
for r in sys.argv[2].splitlines():
    kind, *rest = r.split("|")
    if kind == "ext": ext[rest[0]] = rest[1]
    elif kind == "alembic": alembic = rest[0]
    elif kind == "table": tables[rest[0]] = int(rest[1])
    elif kind == "vectors": vectors[rest[0]] = int(rest[1])
    elif kind == "knn": knn[rest[0]] = int(rest[1])
fails = []
for name, ver in m["extensions"].items():
    if ext.get(name) != ver: fails.append(f"extension {name}: want {ver} got {ext.get(name)}")
if alembic != m["alembic_revision"]: fails.append(f"alembic: want {m['alembic_revision']} got {alembic}")
if tables != m["row_counts"]: fails.append(f"row_counts differ: want {m['row_counts']} got {tables}")
if vectors != m["vector_counts"]: fails.append(f"vector_counts differ: want {m['vector_counts']} got {vectors}")
for col, n in vectors.items():
    if n and knn.get(col, 0) < 1: fails.append(f"pgvector distance query returned nothing for {col}")
print(json.dumps({"extensions": ext, "alembic_revision": alembic, "tables": len(tables),
                  "rows_total": sum(tables.values()), "vector_counts": vectors, "knn_rows": knn}, sort_keys=True))
if fails:
    print("RESTORE VERIFY FAIL:\n  " + "\n  ".join(fails), file=sys.stderr); sys.exit(1)
print("RESTORE VERIFY PASS: extensions, alembic revision, per-table row counts, vector counts, pgvector query")
PY
