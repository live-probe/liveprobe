"""KAN-128: fixed-projection, read-only F2 webhook audit observer.

No raw JSON detail, incident text, credentials, or subprocess output reaches
callers. This is a proof component, not a live fault or CLI entrypoint.
"""
import re
import subprocess

from family2_transaction import UnsafeState, require

# SQL is constant: never interpolate an incident ID, token or caller value.
# Source/reason are projected as bounded literals ONLY after exact matching;
# audit.detail itself is never projected. Both queries are SELECT-only.
BASELINE_SQL = """SELECT COALESCE((SELECT max(id) FROM incidents),0),
 COALESCE((SELECT max(id) FROM audit_log),0),
 extract(epoch from clock_timestamp())::bigint"""
PROOF_SQL = """SELECT i.id, extract(epoch from i.created_at)::bigint,
 a.id, extract(epoch from a.ts)::bigint,
 'alertmanager', 'TargetWorkloadOOMKilled', a.event
 FROM audit_log a JOIN incidents i ON i.id = a.incident_id
 WHERE i.namespace = 'liveprobe-target' AND i.workload = 'inventory'
 AND a.event IN ('incident_created','signal_merged')
 AND a.detail->>'source' = 'alertmanager'
 AND a.detail->>'reason' = 'TargetWorkloadOOMKilled'
 ORDER BY a.id DESC LIMIT 501"""
COUNT_SQL = "SELECT count(*) FROM audit_log"
COMMAND = ("kubectl", "--context", "rancher-desktop", "-n", "liveprobe-system",
           "exec", "postgres-0", "--", "psql", "-U", "liveprobe", "-d", "liveprobe",
           "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c")


def _number(value):
    require(re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value) is not None,
            "audit projection invalid")
    return int(value)


def _rows(result, columns):
    require(result.returncode == 0 and isinstance(result.stdout, str) and
            len(result.stdout) <= 65536, "audit query unavailable (output suppressed)")
    text = result.stdout
    require(text == "" or (text.endswith("\n") and len(text.splitlines()) <= 501),
            "audit projection incomplete")
    rows = [line.split("|") for line in text.splitlines()]
    require(all(len(row) == columns for row in rows), "audit projection invalid")
    return rows


class WebhookAuditObserver:
    """Baseline before fault; prove exact new inventory incident and audit pair.

    A query is an independent read. A stale, duplicate, partial or ambiguous
    result never proves webhook delivery. The caller must separately prove Pod,
    KSM, firing alert and authenticated incident identity.
    """
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run
        self.baseline = None

    def _query(self, sql, columns):
        try:
            result = self.runner([*COMMAND, sql], text=True, capture_output=True,
                                 timeout=12)
            return _rows(result, columns)
        except Exception:
            raise UnsafeState("audit query unavailable (details suppressed)") from None

    def ready(self):
        # Capture high-water marks AND database time before any fault. One row.
        self.baseline = None
        rows = self._query(BASELINE_SQL, 3)
        require(len(rows) == 1, "audit baseline incomplete")
        values = tuple(_number(value) for value in rows[0])
        require(values[2] > 0, "audit baseline invalid")
        self.baseline = values
        return True

    def observe(self, incident_id):
        require(self.baseline is not None and type(incident_id) is int and incident_id > 0,
                "audit observer not armed")
        incident_max, audit_max, baseline_time = self.baseline
        require(incident_id > incident_max, "incident predates audit baseline")
        rows = self._query(PROOF_SQL, 7)
        require(len(rows) < 501, "audit projection truncated")
        parsed = []
        for row in rows:
            iid, created, aid, ts = map(_number, row[:4])
            require(row[4:] in (["alertmanager", "TargetWorkloadOOMKilled", "incident_created"],
                                ["alertmanager", "TargetWorkloadOOMKilled", "signal_merged"]),
                    "audit projection invalid")
            parsed.append((iid, created, aid, ts, row[6]))
        require(len({entry[2] for entry in parsed}) == len(parsed), "duplicate audit ID")
        require(len({entry[0] for entry in parsed if entry[0] > incident_max}) <= 1,
                "ambiguous new inventory incidents")
        matches = [r for r in parsed if r[0] == incident_id and r[2] > audit_max and
                   r[1] >= baseline_time and r[3] >= baseline_time and r[3] >= r[1]]
        require(len(matches) == 1, "new matching webhook audit absent or ambiguous")
        return True

    def count(self):
        """Read-only feasibility check. A count cannot prove F2 delivery."""
        rows = self._query(COUNT_SQL, 1)
        require(len(rows) == 1, "audit count incomplete")
        return _number(rows[0][0])
