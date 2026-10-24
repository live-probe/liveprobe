#!/usr/bin/env bash
# Logical backup of the LiveProbe PostgreSQL+pgvector database (read-only on the live DB).
#
#   scripts/pg-backup.sh                 # -> backups/liveprobe-<UTC timestamp>.dump (+ .manifest.json, .sha256)
#   BACKUP_DIR=/secure/path scripts/pg-backup.sh
#
# Runs pg_dump inside the postgres-0 container over the local socket, so no DB password
# leaves the cluster or is printed. Output is pg_dump custom format (-Fc), which includes
# the `vector` extension, schema, data and the alembic_version row.
# The manifest records extension versions, the Alembic revision and exact per-table row
# counts; scripts/pg-restore.sh verifies a restore against it.
set -euo pipefail
cd "$(dirname "$0")/.."

CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS="${PG_NAMESPACE:-liveprobe-system}"
POD="${PG_POD:-postgres-0}"
DB="${PG_DB:-liveprobe}"
DBUSER="${PG_USER:-liveprobe}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
K=(kubectl --context "$CTX" -n "$NS")

ts="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
out="$BACKUP_DIR/${DB}-${ts}.dump"
tmp="${out}.partial"
trap 'rm -f "$tmp"' EXIT

"${K[@]}" get pod "$POD" >/dev/null

psql_at() { "${K[@]}" exec -i "$POD" -- psql -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=1 -At; }

# pg_dump takes its own snapshot, so these counts come from a separate REPEATABLE READ
# snapshot taken just before the dump. If the app writes in between, restore verification
# reports the difference instead of hiding it.
echo "==> manifest (${CTX}/${NS}/${POD} db=${DB})"
manifest_rows="$(psql_at <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT 'ext|'||extname||'|'||extversion FROM pg_extension ORDER BY extname;
SELECT 'alembic|'||version_num FROM alembic_version;
SELECT 'server|'||current_setting('server_version_num');
SELECT format('select ''table|%s|''||count(*) from %I.%I;', c.relname, n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind='r' AND n.nspname='public' ORDER BY c.relname \gexec
SELECT format('select ''vectors|%s.%s|''||count(%I) from %I.%I;', c.relname, a.attname, a.attname, n.nspname, c.relname)
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_type t ON t.oid=a.atttypid
 WHERE t.typname='vector' AND n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1 \gexec
COMMIT;
SQL
)"

echo "==> pg_dump -Fc"
"${K[@]}" exec "$POD" -- pg_dump -U "$DBUSER" -d "$DB" -Fc --no-owner --no-privileges > "$tmp"
[[ -s "$tmp" ]] || { echo "FAIL: empty dump" >&2; exit 1; }
# A custom-format archive must be listable; this catches truncated transfers.
"${K[@]}" exec -i "$POD" -- pg_restore --list < "$tmp" >/dev/null 2>&1 \
  || { echo "FAIL: dump archive is not readable by pg_restore --list" >&2; exit 1; }
mv "$tmp" "$out"
chmod 600 "$out"

sha="$(shasum -a 256 "$out" | awk '{print $1}')"
echo "$sha  $(basename "$out")" > "${out}.sha256"

python3 - "$out" "$sha" "$CTX" "$NS" "$POD" "$DB" "$ts" "$manifest_rows" <<'PY'
import json, os, sys
out, sha, ctx, ns, pod, db, ts = sys.argv[1:8]
rows = sys.argv[8].splitlines()
m = {"file": os.path.basename(out), "sha256": sha, "bytes": os.path.getsize(out),
     "created_utc": ts, "source": {"context": ctx, "namespace": ns, "pod": pod, "database": db},
     "format": "pg_dump custom (-Fc) --no-owner --no-privileges",
     "extensions": {}, "alembic_revision": None, "server_version_num": None,
     "row_counts": {}, "vector_counts": {}}
for r in rows:
    kind, *rest = r.split("|")
    if kind == "ext": m["extensions"][rest[0]] = rest[1]
    elif kind == "alembic": m["alembic_revision"] = rest[0]
    elif kind == "server": m["server_version_num"] = rest[0]
    elif kind == "table": m["row_counts"][rest[0]] = int(rest[1])
    elif kind == "vectors": m["vector_counts"][rest[0]] = int(rest[1])
with open(out + ".manifest.json", "w") as f:
    json.dump(m, f, indent=2, sort_keys=True)
print(json.dumps({k: m[k] for k in ("file", "bytes", "sha256", "alembic_revision", "extensions", "row_counts", "vector_counts")}, sort_keys=True))
PY
echo "backup OK: $out"
