"""KAN-122 isolated transaction protocol. Not connected to the live CLI.

The caller supplies a credential-safe observer and operator. Never put tokens,
HTTP response bodies, or Secret values in a report or exception from adapters.
A live adapter must coordinate other fault runners before claiming exclusivity.
"""
import uuid

LEASE = "family2-exclusive-fault"
TARGET = "liveprobe-target"
OBS = "liveprobe-observability"
OBJECTS = ((TARGET, "deployment", "inventory"), (OBS, "configmap", "prometheus-config"))


class UnsafeState(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise UnsafeState(message)


def patch_body(current, desired, field):
    """CAS only the owned field, without submitting metadata/status as an apply."""
    metadata = current["metadata"]
    require(metadata["uid"] and metadata["resourceVersion"], "missing CAS identity")
    require(current["metadata"]["name"] == desired["metadata"]["name"] and
            current["metadata"]["namespace"] == desired["metadata"]["namespace"],
            "patch identity mismatch")
    return [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": field, "value": current["spec"]["template"] if field == "/spec/template" else current["data"]},
        {"op": "replace", "path": field, "value": desired["spec"]["template"] if field == "/spec/template" else desired["data"]},
    ]


def field_value(item, index):
    return item["spec"]["template"] if index == 0 else item["data"]


class KubernetesLease:
    """Create-only Lease; existing (including expired) is a hard stop.

    Adapter get/create/delete must enforce context=rancher-desktop and namespace
    liveprobe-target, suppress error bodies, and use UID preconditions on delete.
    No renewal or stale-lease takeover is permitted.
    """
    def __init__(self, adapter, duration_seconds=3600):
        require(type(duration_seconds) is int and 60 <= duration_seconds <= 3600,
                "Lease duration out of bounds")
        self.adapter = adapter
        self.duration_seconds = duration_seconds
        self.uid = None
        self.holder = None

    def acquire(self):
        require(self.uid is None, "lease already held")
        require(self.adapter.get(LEASE) is None, "existing Lease: manual recovery required")
        self.holder = str(uuid.uuid4())
        request = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                   "metadata": {"name": LEASE, "namespace": TARGET},
                   "spec": {"holderIdentity": self.holder, "leaseDurationSeconds": self.duration_seconds}}
        try:
            created = self.adapter.create(request)  # Kubernetes POST, never apply/replace
        except Exception as exc:
            # The server might have accepted the create before a timeout. Do not
            # delete or retry: ownership is uncertain and needs operator review.
            raise UnsafeState("Lease create uncertain: manual review required") from None
        require(created["metadata"]["name"] == LEASE and
                created["metadata"]["namespace"] == TARGET and
                created["spec"]["holderIdentity"] == self.holder and
                created["metadata"].get("uid"), "Lease acknowledgement uncertain")
        self.uid = created["metadata"]["uid"]

    def assert_owned(self):
        current = self.adapter.get(LEASE)
        require(self.uid is not None and current is not None and
                current["metadata"]["uid"] == self.uid and
                current["spec"]["holderIdentity"] == self.holder,
                "Lease ownership lost")

    def release(self):
        self.assert_owned()
        self.adapter.delete(LEASE, uid=self.uid)  # UID precondition, not name-only delete
        self.uid = None


def transaction(operator, observer, lease, backup):
    """Mockable one-shot operation. Not invoked by family2_gate.py --execute.

    Operator methods are deliberately separate from credentials. `fault` may
    perform only a six-second v1 native OOM on a loopback-forwarded Pod UID;
    `observe` must verify OOMKilled/137, KSM, firing alert and incident.
    Snapshot must be fsynced to a private 0600 file before first mutation.
    """
    modified = False
    restored = False
    fault_attempted = False
    fault_uncertain = False
    lease.acquire()
    try:
        lease.assert_owned()
        require(operator.exclusive_window(), "another fault worker may be active")
        before, desired, report = operator.preflight()
        require(len(before) == len(desired) == 2 and
                all((item["metadata"]["namespace"], item["kind"].lower(), item["metadata"]["name"]) == key
                    for item, key in zip(before, OBJECTS)), "inventory shape changed")
        require(observer.ready(), "authenticated loopback incident observer unavailable")
        require(operator.faults_clear(), "pending fault exists")
        backup(before, desired)  # exclusive, durable, private snapshot or raise
        # Re-read both identities and fields just before the first write.
        for index, key in enumerate(OBJECTS):
            current = operator.get(*key)
            require(current["metadata"]["uid"] == before[index]["metadata"]["uid"] and
                    current["metadata"]["resourceVersion"] == before[index]["metadata"]["resourceVersion"] and
                    field_value(current, index) == field_value(before[index], index),
                    "snapshot drift before mutation")
        lease.assert_owned()
        try:
            for index, key in enumerate(OBJECTS):
                if field_value(desired[index], index) == field_value(before[index], index):
                    # KAN-134: already-desired object (e.g. live OOM rule is
                    # already 0s) is a no-op: no write, nothing to restore.
                    continue
                # Mark as potentially changed BEFORE a call that may time out
                # after server-side success. Finally must inspect both objects.
                modified = True
                operator.patch(*key, patch_body(before[index], desired[index],
                               "/spec/template" if index == 0 else "/data"))
            operator.rollout("inventory")
            operator.rollout("prometheus")
            operator.verify_candidate(desired, report)
            require(operator.faults_clear(), "pending fault before injection")
            # KAN-131: exclusive window is re-checked immediately before POST,
            # while nothing has been attempted yet (no uncertain ownership).
            require(operator.exclusive_window(), "exclusive window lost before POST")
            lease.assert_owned()
            fault_pod_uid = operator.current_pod_uid()
            if hasattr(observer, "bind"):
                observer.bind(fault_pod_uid)
            fault_attempted = True  # POST may be accepted before a transport timeout
            try:
                operator.fault(mode="oom", duration_seconds=6, pod_uid=fault_pod_uid)
            except Exception:
                fault_uncertain = True
                raise UnsafeState("fault POST uncertain: Lease retained") from None
            require(observer.observe(report), "OOM/alert/webhook/incident chain unverified")
        finally:
            if modified:
                # A partial patch must still restore even if the fault was never
                # attempted. Once POST was attempted, reset is mandatory.
                clear_error = None
                if fault_attempted:
                    try:
                        operator.clear_fault()
                    except Exception:
                        clear_error = UnsafeState("fault reset unavailable")
                restore_errors = []
                for index, key in reversed(list(enumerate(OBJECTS))):
                    try:
                        current = operator.get(*key)
                        if field_value(current, index) == field_value(before[index], index):
                            continue
                        require(current["metadata"]["uid"] == before[index]["metadata"]["uid"] and
                                field_value(current, index) == field_value(desired[index], index),
                                "concurrent modification: leave Lease and snapshot for manual recovery")
                        operator.patch(*key, patch_body(current, before[index],
                                       "/spec/template" if index == 0 else "/data"))
                    except Exception:
                        # Continue with the other object, but retain the Lease.
                        restore_errors.append(index)
                if restore_errors:
                    raise UnsafeState("rollback incomplete: Lease retained; manual recovery required")
                operator.rollout("inventory")
                operator.rollout("prometheus")
                operator.verify_restored(before, report)
                require(clear_error is None, "fault reset failed: Lease retained")
                require(operator.faults_clear(), "pending fault after restoration")
                lease.assert_owned()
                restored = True
        return {"result": "OFFLINE_TRANSACTION_VERIFIED", "restored": restored}
    finally:
        # If any post-write verification failed, retain Lease for manual
        # escalation; never imply restoration was verified merely by a patch.
        if (not modified or restored) and not fault_uncertain:
            lease.release()
