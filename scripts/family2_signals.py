"""Fail-closed Family 2 signal readers and loopback fault boundary.

No credentials, port-forward, or cluster writes are supplied by this module.
An independent authority must approve and coordinate any eventual live runner.
"""
import re
import time
import urllib.parse

from family2_transaction import UnsafeState, require

ALERT = "TargetWorkloadOOMKilled"
NAMESPACE = "liveprobe-target"
PROM_QUERIES = {
    "ksm": 'kube_pod_container_status_last_terminated_reason{namespace="liveprobe-target",container="inventory",reason="OOMKilled"}',
    "firing": 'ALERTS{alertname="TargetWorkloadOOMKilled",namespace="liveprobe-target",alertstate="firing"}',
}


def _sample(payload, labels, expected_value=None):
    require(payload.get("status") == "success" and payload.get("data", {}).get("resultType") == "vector",
            "Prometheus vector unavailable")
    matches = []
    for item in payload["data"]["result"]:
        metric = item["metric"]
        if all(metric.get(k) == v for k, v in labels.items()):
            value = item.get("value", [])
            require(len(value) == 2 and isinstance(value[1], str), "invalid metric sample")
            try:
                measured = float(value[1])
            except ValueError:
                raise UnsafeState("invalid metric sample") from None
            if expected_value is None or measured == expected_value:
                matches.append((metric, measured))
    return matches


class SignalReader:
    """Injected loopback GET returns decoded JSON, never logs response bodies."""
    def __init__(self, get_json):
        self.get_json = get_json

    def query(self, expression, labels, value=1):
        path = "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
        return _sample(self.get_json("prometheus", path), labels, value)

    def rule_loaded(self, duration):
        require(duration in ("0s", "30s"), "invalid rule duration")
        payload = self.get_json("prometheus", "/api/v1/rules")
        require(payload.get("status") == "success", "Prometheus rules unavailable")
        rules = [r for group in payload.get("data", {}).get("groups", [])
                 for r in group.get("rules", []) if r.get("name") == ALERT]
        return (len(rules) == 1 and rules[0].get("type") == "alerting" and
                rules[0].get("health") == "ok" and
                rules[0].get("duration") == (0 if duration == "0s" else 30) and
                'reason="OOMKilled"' in rules[0].get("query", ""))

    def firing_for(self, pod):
        require(re.fullmatch(r"inventory-[a-z0-9-]+", pod) is not None, "invalid Pod name")
        labels = {"alertname": ALERT, "namespace": NAMESPACE, "pod": pod, "alertstate": "firing"}
        return bool(self.query(PROM_QUERIES["firing"], labels))

    def oom_for(self, pod):
        require(re.fullmatch(r"inventory-[a-z0-9-]+", pod) is not None, "invalid Pod name")
        return bool(self.query(PROM_QUERIES["ksm"],
                               {"namespace": NAMESPACE, "pod": pod, "container": "inventory",
                                "reason": "OOMKilled"}))


def terminated_oom(pod, original_uid, original_restart):
    """Require an actual Kubernetes OOMKilled/137 on the exact faulted Pod."""
    require(pod.get("metadata", {}).get("uid") == original_uid, "fault Pod UID changed")
    statuses = [s for s in pod.get("status", {}).get("containerStatuses", [])
                if s.get("name") == "inventory"]
    if len(statuses) != 1:
        return False
    status = statuses[0]
    reason = status.get("lastState", {}).get("terminated", {})
    return (status.get("restartCount", 0) > original_restart and
            reason.get("reason") == "OOMKilled" and reason.get("exitCode") == 137)


class ChainObserver:
    """Poll injected authenticated incident reads after true Pod/KSM/alert proof.

    `list_incidents` and `get_incident` must use an independently approved
    authenticated loopback session. No login, token, or response is logged here.
    """
    def __init__(self, signals, pod_reader, list_incidents, get_incident,
                 timeout: float=120, interval: float=2, audit=None):
        require(0 < timeout <= 180 and 0 < interval <= 10, "unbounded observer")
        self.signals = signals
        self.pod_reader = pod_reader
        self.list_incidents = list_incidents
        self.get_incident = get_incident
        self.audit = audit
        self.pod_name = None
        self.pod_uid = None
        self.restart_count = None
        self.timeout = timeout
        self.interval = interval
        self.existing_ids = None
        self.incident_id = None

    def bind(self, pod_uid):
        require(self.existing_ids is not None and self.pod_uid is None, "observer not armed")
        pod = self.pod_reader()
        require(pod.get("metadata", {}).get("uid") == pod_uid and
                re.fullmatch(r"inventory-[a-z0-9-]+", pod["metadata"].get("name", "")),
                "fault Pod identity mismatch")
        statuses = [s for s in pod.get("status", {}).get("containerStatuses", [])
                    if s.get("name") == "inventory"]
        require(len(statuses) == 1 and statuses[0].get("ready") is True and
                type(statuses[0].get("restartCount")) is int, "fault Pod not ready")
        self.pod_name = pod["metadata"]["name"]
        self.pod_uid = pod_uid
        self.restart_count = statuses[0]["restartCount"]
        payload = self.list_incidents()
        require(isinstance(payload, dict) and isinstance(payload.get("items"), list),
                "authenticated incident baseline unavailable")
        self.existing_ids = {i["id"] for i in payload["items"]}
        require(self.audit is not None and self.audit.ready() is True,
                "webhook audit baseline unavailable")

    def ready(self):
        try:
            payload = self.list_incidents()
            require(isinstance(payload, dict) and isinstance(payload.get("items"), list),
                    "authenticated incident list unavailable")
            self.existing_ids = {i["id"] for i in payload["items"]}
            require(self.audit is not None and self.audit.ready() is True,
                    "webhook audit baseline unavailable")
            return True  # candidate rule is verified after scoped rollout
        except Exception:
            return False

    def observe(self, report):
        require(self.existing_ids is not None and self.pod_uid is not None and
                report.get("pod_uid") != self.pod_uid,
                "observer not bound to a freshly rolled-out fault Pod")
        deadline = time.monotonic() + self.timeout
        observed = {"pod": False, "ksm": False, "alert": False}
        while time.monotonic() < deadline:
            try:
                observed["pod"] |= terminated_oom(self.pod_reader(), self.pod_uid, self.restart_count)
                observed["ksm"] |= self.signals.oom_for(self.pod_name)
                observed["alert"] |= self.signals.firing_for(self.pod_name)
                if all(observed.values()):
                    for entry in self.list_incidents()["items"]:
                        iid = entry["id"]
                        if iid in self.existing_ids:
                            continue
                        detail = self.get_incident(iid)
                        incident = detail.get("incident", detail)
                        if (incident.get("namespace") == NAMESPACE and
                                incident.get("workload") == "inventory" and
                                ALERT in incident.get("signal_reasons", []) and
                                incident.get("id") == iid and
                                self.audit is not None and self.audit.observe(iid) is True):
                            self.incident_id = iid
                            return True
            except Exception:
                # Transient reads do not prove success; retry only until deadline.
                pass
            time.sleep(min(self.interval, max(0, deadline - time.monotonic())))
        return False


class LoopbackFault:
    """Disabled legacy boundary: it assembled Authorization on the host.

    Use family2_opaque.OpaquePodFault only within a separately approved,
    isolated transaction. Retained solely to reject stale consumers explicitly.
    """
    def __init__(self, *args, **kwargs):
        raise UnsafeState("host-token loopback fault boundary disabled")
