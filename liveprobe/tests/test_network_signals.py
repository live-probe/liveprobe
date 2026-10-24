"""P14 B5: real Hubble/Retina records -> bounded signals -> safe incident routing."""
from __future__ import annotations

import pytest

from app.config import AppConfig
from app.flows import FlowRec, FlowStore, flow_from_proto
from app.router import decide
from app.safety import effective_policy
from conftest import family1_kube, needs_db
from test_router_workflows import family1

NS = "liveprobe-target"
SRC = f"{NS}/pod/inventory-aaa-1"
DST = f"{NS}/pod/inventory-bbb-1"
PEER = "ip/203.0.113.9"


def setup_adapter(emit=None, *, merge=10, window=300):
    from app import network_signals
    now = [1000.0]
    store = FlowStore(window_s=window, clock=lambda: now[0])
    store.stream_state = "connected"
    kube = family1_kube()
    for pod in kube.pods:
        pod["metadata"]["namespace"] = NS
    sent = []
    adapter = network_signals.NetworkSignalAdapter(
        store, kube, AppConfig(merge_window_seconds=merge), emit or sent.append,
        clock=lambda: now[0],
    )
    return adapter, store, kube, sent, now


def records(n=5, *, src=PEER, dst=DST, ts=999.0, verdict="DROPPED"):
    return [FlowRec(ts + i / 1000, src, dst, verdict, "TCP", 8080,
                    "POLICY_DENIED" if verdict == "DROPPED" else None) for i in range(n)]


def test_real_hubble_proto_drop_and_error_emit_target_deployment():
    adapter, store, kube, sent, now = setup_adapter()
    from app.hubble_gen.flow import flow_pb2 as pb
    batch = []
    for i in range(5):
        flow = pb.Flow(verdict=pb.DROPPED if i < 3 else pb.ERROR,
                       drop_reason_desc=pb.POLICY_DENIED)
        flow.time.seconds, flow.time.nanos = 999, i
        flow.IP.source, flow.IP.destination = "203.0.113.9", "10.42.0.8"
        flow.destination.namespace, flow.destination.pod_name = NS, "inventory-bbb-1"
        flow.l4.TCP.destination_port = 8080
        batch.append(flow_from_proto(flow))
    store.add(batch)
    assert adapter.poll_once() == 1
    assert len(sent) == 1
    sig = sent[0]
    assert (sig.namespace, sig.kind, sig.name, sig.reason, sig.source) == (
        NS, "Deployment", "inventory", "NetworkDrop", "hubble")
    assert (sig.detail["dropped"], sig.detail["errors"]) == (3, 2)
    assert sig.detail["from"] == PEER
    assert sig.detail["to"] == f"{NS}/Deployment/inventory"
    assert sig.detail["drop_reasons"] == {"POLICY_DENIED": 3}
    assert sig.detail["window_s"] == 300
    assert not kube.patches


def test_same_pair_once_per_merge_window_requires_five_new_bad_records():
    adapter, store, _, sent, now = setup_adapter()
    store.add(records())
    assert adapter.poll_once() == 1
    assert adapter.poll_once() == 0
    now[0] += 2
    store.add(records(ts=now[0] - 1))
    assert adapter.poll_once() == 0  # new drops during the merge window coalesce
    now[0] = 1010
    assert adapter.poll_once() == 1  # a new window with five genuinely new drops
    assert len(sent) == 2
    now[0] = 1020
    store.add(records(1, ts=1019))
    assert adapter.poll_once() == 0  # old 10 + new 1 must not become another incident
    store.add(records(4, ts=1019.1))
    assert adapter.poll_once() == 1
    assert len(sent) == 3


@pytest.mark.parametrize("batch", [
    [], records(20, verdict="FORWARDED"), records(4),
    records(dst="other/pod/inventory-bbb-1"),
    records(src="ip/10.1.1.1", dst="ip/10.1.1.2"),
    records(src=SRC, dst=DST),  # same Deployment's internal traffic is not a path edge
])
def test_normal_below_threshold_other_namespace_unknown_and_self_edges_do_not_emit(batch):
    adapter, store, _, sent, _ = setup_adapter()
    store.add(batch)
    assert adapter.poll_once() == 0
    assert sent == []


@pytest.mark.parametrize("state,age", [("connected", 61), ("reconnecting", 1), ("never_connected", 1)])
def test_stale_or_disconnected_observation_cannot_create_an_incident(state, age):
    adapter, store, _, sent, now = setup_adapter()
    store.add(records())
    store.stream_state = state
    now[0] += age
    assert adapter.poll_once() == 0
    assert sent == []


def test_silence_or_forwarded_heartbeat_cannot_reuse_old_bad_counts():
    adapter, store, _, sent, now = setup_adapter()
    store.add(records())
    assert adapter.poll_once() == 1
    now[0] += 11
    assert adapter.poll_once() == 0
    store.add(records(20, ts=now[0] - 1, verdict="FORWARDED"))
    assert adapter.poll_once() == 0
    now[0] += 400
    store.add(records(1, ts=now[0] - 1))
    assert adapter.poll_once() == 0
    assert len(sent) == 1
    # An empty snapshot did not assert recovery or reset the suppression watermark.
    store.add(records(4, ts=now[0] - 0.5))
    assert adapter.poll_once() == 1


def test_emit_failure_isolated_by_pair_and_failed_pair_can_retry():
    sent = []
    fail = [True]
    def emit(sig):
        if sig.detail["from"] == PEER and fail[0]:
            raise RuntimeError("database unavailable")
        sent.append(sig)
    adapter, store, _, _, _ = setup_adapter(emit)
    store.add(records() + records(src="ip/203.0.113.10"))
    assert adapter.poll_once() == 1
    assert len(sent) == 1 and sent[0].detail["from"] == "ip/203.0.113.10"
    assert adapter.last_errors == [{"stage": "emit", "error": "RuntimeError"}]
    fail[0] = False
    assert adapter.poll_once() == 1
    assert len(sent) == 2 and sent[1].detail["from"] == PEER
    assert adapter.last_errors == []


def test_one_pod_owner_lookup_failure_does_not_block_other_target(monkeypatch):
    adapter, store, kube, sent, _ = setup_adapter()
    original = kube.get_replicaset
    def get_rs(ns, name):
        if name == "inventory-aaa":
            raise RuntimeError("one owner unavailable")
        return original(ns, name)
    monkeypatch.setattr(kube, "get_replicaset", get_rs)
    store.add(records())
    assert adapter.poll_once() == 1
    assert sent[0].name == "inventory"
    assert adapter.last_errors == [{"stage": "pod_owner", "error": "RuntimeError"}]


@pytest.mark.parametrize("failing", ["snapshot", "list_pods"])
def test_source_failure_retains_watermark_and_can_retry(monkeypatch, failing):
    adapter, store, kube, sent, _ = setup_adapter()
    store.add(records())
    target = store if failing == "snapshot" else kube
    original = getattr(target, failing)
    def fail(*args):
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(target, failing, fail)
    assert adapter.poll_once() == 0
    assert sent == [] and adapter.last_errors
    monkeypatch.setattr(target, failing, original)
    assert adapter.poll_once() == 1


def test_reconnect_replay_cannot_turn_two_bad_observations_into_five():
    adapter, store, _, sent, _ = setup_adapter()
    batch = records(2)
    store.add(batch + batch + batch)
    assert adapter.poll_once() == 0
    assert sent == []
    store.add(records(3, ts=999.1))
    assert adapter.poll_once() == 1


def test_hubble_uuid_preserves_distinct_same_timestamp_flows_and_dedupes_replay():
    adapter, store, _, sent, _ = setup_adapter()
    from app.hubble_gen.flow import flow_pb2 as pb
    batch = []
    for i in range(5):
        flow = pb.Flow(verdict=pb.DROPPED, uuid=f"flow-{i}")
        flow.time.seconds = 999
        flow.IP.source = "203.0.113.9"
        flow.destination.namespace, flow.destination.pod_name = NS, "inventory-bbb-1"
        batch.append(flow_from_proto(flow))
    store.add(batch + batch)
    assert adapter.poll_once() == 1
    assert sent[0].detail["dropped"] == 5
    assert [f.flow_id for f in batch] == [f"flow-{i}" for i in range(5)]


def test_retina_ip_only_pods_and_service_ip_fold_into_same_workload_pair():
    adapter, store, kube, sent, _ = setup_adapter()
    kube.pods[1]["status"]["podIP"] = "10.42.0.8"
    kube.services["inventory"]["metadata"]["namespace"] = NS
    kube.services["inventory"]["spec"]["clusterIP"] = "10.43.0.8"
    store.add(records(2, dst="ip/10.42.0.8") + records(3, dst="ip/10.43.0.8", ts=999.1))
    assert adapter.poll_once() == 1
    assert sent[0].detail["to"] == f"{NS}/Deployment/inventory"
    assert sent[0].detail["dropped"] == 5


def test_ambiguous_service_selector_does_not_guess_a_workload():
    adapter, store, kube, sent, _ = setup_adapter()
    from conftest import mk_rs
    kube.replicasets.append(mk_rs("order-ccc", "order", 1, "order:ok"))
    kube.services["inventory"]["spec"].update(clusterIP="10.43.0.8", selector={"shared": "yes"})
    for pod in kube.pods:
        pod["metadata"]["labels"]["shared"] = "yes"
    store.add(records(dst="ip/10.43.0.8"))
    assert adapter.poll_once() == 0
    assert sent == []


def test_watcher_optional_flow_adapter_polls_and_has_separate_status():
    from app.watcher import Watchers
    _, store, kube, sent, _ = setup_adapter()
    watchers = Watchers(kube, AppConfig(), sent.append, flow_store=store)
    # Use the same deterministic event-time domain as the test FlowStore.
    assert watchers.network is not None
    watchers.network.clock = store.clock
    store.add(records())
    assert watchers.poll_network_once() == 1
    assert watchers.status["network_poller"]["state"] == "running"
    assert sent[0].reason == "NetworkDrop"
    store.stream_state = "reconnecting"
    assert watchers.poll_network_once() == 0
    assert watchers.status["network_poller"]["state"] == "stale"


def test_watcher_starts_network_thread_only_when_enabled(monkeypatch):
    from app import watcher
    _, store, kube, _, _ = setup_adapter()
    names = []
    class Thread:
        def __init__(self, *, name, **kwargs):
            self.name = name
        def start(self):
            names.append(self.name)
    monkeypatch.setattr(watcher.threading, "Thread", Thread)
    watcher.Watchers(kube, AppConfig(), lambda _: None).start()
    assert "network_poller" not in names
    watcher.Watchers(kube, AppConfig(), lambda _: None, flow_store=store).start()
    assert names.count("network_poller") == 1


@pytest.mark.parametrize("ts", [500.0, 1100.0])
def test_out_of_order_expired_or_future_records_cannot_make_threshold(ts):
    adapter, store, _, sent, _ = setup_adapter()
    # A fresh first entry means FlowStore may still retain out-of-order old entries.
    store.add(records(1, verdict="FORWARDED") + records(ts=ts))
    assert adapter.poll_once() == 0
    assert sent == []


def test_target_source_is_selected_for_blocked_egress():
    adapter, store, _, sent, _ = setup_adapter()
    store.add(records(src=DST, dst=PEER))
    assert adapter.poll_once() == 1
    assert sent[0].name == "inventory"
    assert sent[0].detail["from"] == f"{NS}/Deployment/inventory"


@pytest.mark.parametrize("invalid", ["orphan", "deleting", "foreign"])
def test_targets_must_have_current_in_namespace_deployment_owner(invalid):
    adapter, store, kube, sent, _ = setup_adapter()
    if invalid == "orphan":
        kube.pods[1]["metadata"]["ownerReferences"] = []
    elif invalid == "deleting":
        kube.pods[1]["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
    else:
        kube.pods[1]["metadata"]["namespace"] = "other"
    store.add(records())
    assert adapter.poll_once() == 0
    assert sent == []


def test_concurrent_pollers_cannot_emit_the_same_pair_twice():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    sent = []
    ready = threading.Barrier(2)
    def emit(sig):
        time.sleep(0.05)  # let another poller reach the same unconsumed pair
        sent.append(sig)
    adapter, store, _, _, _ = setup_adapter(emit)
    store.add(records())
    def poll():
        ready.wait(timeout=2)
        return adapter.poll_once()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: poll(), range(2)))
    assert sorted(results) == [0, 1]
    assert len(sent) == 1


def test_network_drop_event_also_has_a_deterministic_high_risk_rule():
    from app.router import extract_features, rules_decide
    bundle = family1()
    bundle.events = [{"reason": "NetworkDrop"}]
    assert rules_decide(extract_features(bundle))[0] == "HIGH_RISK"


def test_consumed_pair_bookkeeping_is_bounded_without_rearming_old_errors():
    adapter, store, _, sent, now = setup_adapter(merge=600)
    store.add(records())
    assert adapter.poll_once() == 1
    now[0] = 1500
    store.add(records(1, ts=1499, verdict="FORWARDED"))
    assert adapter.poll_once() == 0
    assert len(adapter._emitted) == 1  # still inside the merge horizon
    now[0] = 1901
    store.add(records(1, ts=1900, verdict="FORWARDED"))
    assert adapter.poll_once() == 0
    assert adapter._emitted == {}  # neither live records nor merge suppression need this pair
    assert len(sent) == 1


def test_network_drop_rules_override_simple_and_llm_downgrade():
    decision = decide(family1(), 15, lambda _: {
        "level": "SIMPLE", "rationale": "pods are ready", "confidence": 1.0,
    }, signal_reasons=["NetworkDrop"])
    assert decision.level == "HIGH_RISK"
    assert decision.workflow == "high_risk_group"
    assert decision.features["network_drop_signal"] is True
    assert "NetworkDrop" in decision.rationale and "rules win" in decision.rationale
    assert effective_policy(decision.level, "auto") == "handoff"


@needs_db
def test_flow_signal_ingestion_routes_high_risk_and_hands_off_without_actions(db_url, monkeypatch):
    from sqlalchemy import select
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.evidence import EvidenceItem
    from app.models import Action, Incident, RoutingDecision
    from app.workflows import AgentCall

    adapter, store, kube, _, _ = setup_adapter()
    engine = Engine(AppConfig(settle_seconds=0), Settings(), kube, None, None)
    monkeypatch.setattr(engine_module, "evaluation_lock_active", lambda: False)
    bundle = family1()
    bundle.items = [EvidenceItem("deployment", "inventory", bundle.deployment),
                    EvidenceItem("pod_status", "inventory", bundle.pods)]
    monkeypatch.setattr(engine_module, "collect", lambda *args, **kwargs: bundle)
    monkeypatch.setattr(engine, "_classify", lambda *args: {
        "level": "SIMPLE", "rationale": "ignore network drops", "confidence": 1.0,
    })
    roles = []
    def agent(role, prompt):
        roles.append(role)
        return AgentCall(True, {"summary": "inspect observed dropped traffic", "confidence": 1.0,
                               "root_cause_candidates": [], "recommended_steps": ["inspect network policy"],
                               "next_action": "rollout_undo"}, len(roles))
    monkeypatch.setattr(engine, "_agent_runner", lambda *args, **kwargs: agent)
    adapter.emit = engine.on_signal
    store.add(records())
    assert adapter.poll_once() == 1
    assert adapter.poll_once() == 0
    with db.session() as session:
        incidents = list(session.scalars(select(Incident)))
        assert len(incidents) == 1
        iid = incidents[0].id
        assert incidents[0].signal_reasons == ["NetworkDrop"]
        assert incidents[0].signal_count == 1
        assert incidents[0].workload == "inventory"
    engine.process(iid)
    with db.session() as session:
        incident = session.get(Incident, iid)
        routing = session.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
        assert incident is not None and routing is not None
        assert incident.status == "handed_off"
        assert incident.severity_level == routing.level == "HIGH_RISK"
        assert routing.decided_by == "rules+llm"
        assert routing.features["network_drop_signal"]
        assert session.scalar(select(Action).where(Action.incident_id == iid)) is None
    assert roles == ["evidence_curator", "safety_verifier"]
    assert kube.patches == []


@pytest.mark.parametrize("method", ["get_replicaset", "list_services"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_network_poll_budget_is_bounded_degraded_retryable_and_has_no_late_effects(monkeypatch, method, late_failure):
    import copy
    import threading
    import time
    from app import network_signals
    from app.watcher import Watchers

    monkeypatch.setattr(network_signals, "POLL_TIMEOUT_S", 0.04, raising=False)
    _, store, kube, sent, _ = setup_adapter()
    watchers = Watchers(kube, AppConfig(), sent.append, flow_store=store)
    adapter = watchers.network
    assert adapter is not None
    adapter.clock = store.clock
    store.add(records())
    release, drained = threading.Event(), threading.Event()
    calls = []
    original = getattr(kube, method)
    def blocked(*args):
        calls.append(method)
        release.wait(0.5)
        if late_failure:
            raise RuntimeError("late observation failure")
        return original(*args)
    monkeypatch.setattr(kube, method, blocked)
    targets = adapter._targets
    def observed_targets(*args, **kwargs):
        try:
            return targets(*args, **kwargs)
        finally:
            drained.set()
    monkeypatch.setattr(adapter, "_targets", observed_targets)
    try:
        started = time.monotonic()
        emitted = watchers.poll_network_once()
        assert time.monotonic() - started < 0.25
        assert emitted == 0 and sent == [] and adapter._emitted == {}
        assert watchers.status["network_poller"]["state"] == "degraded"
        assert any(e["error"] == "TimeoutError" for e in adapter.last_errors)
        before = len(calls)
        for _ in range(3):
            assert watchers.poll_network_once() == 0
        assert len(calls) == before  # one outstanding read, not one new worker per retry
        snapshot = copy.deepcopy((adapter.last_errors, adapter._emitted, watchers.status, sent))
    finally:
        release.set()
    assert drained.wait(1)
    time.sleep(0.03)
    assert (adapter.last_errors, adapter._emitted, watchers.status, sent) == snapshot
    assert len(calls) == before  # an expired read cannot start more owner/service requests
    monkeypatch.setattr(kube, method, original)
    assert watchers.poll_network_once() == 1
    assert watchers.status["network_poller"]["state"] == "running"
    assert len(sent) == 1


def test_network_poll_budget_includes_wait_for_another_poll(monkeypatch):
    import threading
    import time
    from app import network_signals

    monkeypatch.setattr(network_signals, "POLL_TIMEOUT_S", 0.04, raising=False)
    adapter, store, _, sent, _ = setup_adapter()
    store.add(records())
    adapter._lock.acquire()
    release = threading.Timer(0.3, adapter._lock.release)
    release.start()
    try:
        started = time.monotonic()
        assert adapter.poll_once() == 0
        assert time.monotonic() - started < 0.2
        assert sent == []
    finally:
        release.join(timeout=1)
    assert adapter.poll_once() == 1


@pytest.mark.parametrize("late_failure", [False, True])
def test_slow_delivery_is_bounded_pending_and_never_duplicated(monkeypatch, late_failure):
    import copy
    import threading
    import time
    from app import network_signals
    from app.watcher import Watchers

    monkeypatch.setattr(network_signals, "POLL_TIMEOUT_S", 0.04)
    _, store, kube, _, _ = setup_adapter()
    release, drained = threading.Event(), threading.Event()
    started_calls, sent = [], []

    def emit(sig):
        started_calls.append(sig)
        if len(started_calls) == 1:
            release.wait(1)
            drained.set()
            if late_failure:
                raise RuntimeError("delivery failed")
        sent.append(sig)

    watchers = Watchers(kube, AppConfig(), emit, flow_store=store)
    adapter = watchers.network
    assert adapter is not None
    adapter.clock = store.clock
    store.add(records())
    # Lets the original synchronous implementation finish its intended RED.
    guard = threading.Timer(0.35, release.set)
    guard.start()
    try:
        started = time.monotonic()
        assert watchers.poll_network_once() == 0
        assert time.monotonic() - started < 0.2
        assert watchers.status["network_poller"]["state"] == "degraded"
        assert adapter._emitted == {} and len(started_calls) == 1
        for _ in range(2):
            assert watchers.poll_network_once() == 0
        assert len(started_calls) == 1
        snapshot = copy.deepcopy((adapter.last_errors, adapter._emitted, watchers.status))
        release.set()
        assert drained.wait(1)
        time.sleep(0.01)
        assert (adapter.last_errors, adapter._emitted, watchers.status) == snapshot
        # Only a known failure can retry; uncertain delivery stays pending.
        assert watchers.poll_network_once() == 1
        assert len(sent) == 1
        assert len(started_calls) == (2 if late_failure else 1)
        assert adapter.poll_once() == 0
        assert adapter.last_errors == []
    finally:
        release.set()
        guard.cancel()
        guard.join(timeout=1)
