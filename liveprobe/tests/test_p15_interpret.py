"""P15 W2/W3: Korean role catalog, raw-item extraction (verbatim) and rule-based Korean readings."""
import json

import pytest

SAMPLE_BUNDLE = {
    "E1_deployment": {"name": "inventory", "images": ["liveprobe/inventory:does-not-exist-01"], "replicas": 1,
                      "ready_replicas": 0, "available_replicas": 0,
                      "conditions": [{"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated"}],
                      "resources": {"inventory": {"limits": {"memory": "512Mi"}}}},
    "E2_pods": [{"name": "inventory-abc", "phase": "Pending", "waiting_reasons": ["ImagePullBackOff"],
                 "containers": [{"name": "inventory", "waiting_reason": "ImagePullBackOff", "restart_count": 0,
                                 "last_terminated_reason": None}]},
                {"name": "inventory-def", "phase": "Running", "containers": [
                    {"name": "inventory", "waiting_reason": "CrashLoopBackOff", "last_terminated_reason": "OOMKilled",
                     "restart_count": 4}]}],
    "E3_service": {"name": "inventory", "selector": {"app": "inventory"}, "ready_endpoints": 0},
    "E4_callers": {"order": {"http_5xx_rate_per_s": 2.5}},
    "E5_recent_events": [
        {"last": "2026-10-11T12:02:02+00:00", "type": "Warning", "count": 4, "object": "Pod/inventory-abc",
         "reason": "Failed", "message": "Failed to pull image \"liveprobe/inventory:does-not-exist-01\": not found"},
        {"last": "2026-10-11T12:01:00+00:00", "type": "Normal", "count": 1, "object": "Pod/inventory-abc",
         "reason": "Scheduled", "message": "Successfully assigned liveprobe-target/inventory-abc to node"},
        {"last": "2026-10-11T12:03:00+00:00", "type": "Warning", "count": 2, "object": "Pod/inventory-def",
         "reason": "BackOff", "message": "Back-off restarting failed container inventory in pod inventory-def"},
    ],
    "E6_metrics": {"http_rate_per_s": {"order": 4.0}, "http_5xx_rate_per_s": {"order": 2.5},
                   "mean_latency_ms_1m": {"order": 1200.0}, "db_pool_active": {"order": 10}, "db_pool_max": {"order": 10},
                   "db_pool_pending_threads": {"order": 6}},
    "E7_error_logs": ["[order] {\"@timestamp\":\"2026-10-11T12:17:24.978Z\",\"log\":{\"level\":\"WARN\"},"
                      "\"message\":\"order db lookup failed item=widget error=CannotGetJdbcConnectionException\"}",
                      "[order] inventory call failed item=widget error=ServiceUnavailable: 503 Service Unavailable: "
                      "\"no healthy upstream\""],
    "E8_node": [{"name": "node-1", "conditions": {"Ready": {"status": "True"}}}],
    "E9_config": [{"name": "inventory-config", "keys": ["A"], "data_sha256": "x" * 64}],
}


def test_every_workflow_role_has_korean_name_job_and_inputs():
    from app.agent_groups import GROUP_ROLES
    from app.roles_ko import ROLES, role_info
    roles = {"router", "signal_analyst", "result_verifier", "evidence_curator", "safety_verifier", "interpreter",
             "steering", "operator_planner"} | {r for rs in GROUP_ROLES.values() for r in rs}
    for r in roles:
        info = role_info(r)
        assert info["role_ko"] and info["job_ko"], r
        assert all("\uac00" <= ch <= "\ud7a3" or not ch.isalpha() or ch.isascii() for ch in info["job_ko"])
        assert any("\uac00" <= ch <= "\ud7a3" for ch in info["role_ko"]), r
    assert set(ROLES) >= roles
    unknown = role_info("not_a_role")
    assert unknown["role_ko"] == "not_a_role" and unknown["job_ko"]


def test_raw_items_keep_text_verbatim_and_have_stable_ids():
    from app.interpret import raw_items
    items = raw_items(SAMPLE_BUNDLE)
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    ev = [i for i in items if i["ref"] == "E5_recent_events"]
    assert len(ev) == 3 and ev[0]["kind"] == "event" and ev[0]["source"] == "kubernetes"
    # verbatim: the original message is contained unchanged
    assert any(SAMPLE_BUNDLE["E5_recent_events"][0]["message"] in i["raw"] for i in ev)
    logs = [i for i in items if i["ref"] == "E7_error_logs"]
    assert [l["raw"] for l in logs] == SAMPLE_BUNDLE["E7_error_logs"]
    assert {i["kind"] for i in items} >= {"event", "log", "status", "metric", "config", "node"}
    # warnings/errors first-class
    sev = {i["id"]: i["severity"] for i in items}
    assert sev[ev[0]["id"]] == "error" or sev[ev[0]["id"]] == "warn"
    assert all(i["severity"] in ("info", "warn", "error") for i in items)
    # second call yields identical ids (stable)
    assert ids == [i["id"] for i in raw_items(json.loads(json.dumps(SAMPLE_BUNDLE)))]


@pytest.mark.parametrize("needle,expect", [
    ("Failed to pull image", "이미지"),
    ("Back-off restarting failed container", "재시작"),
    ("CannotGetJdbcConnectionException", "DB 연결"),
    ("no healthy upstream", "정상 대상"),
])
def test_rule_readings_cover_known_failure_patterns(needle, expect):
    from app.interpret import raw_items, rule_ko
    item = next(i for i in raw_items(SAMPLE_BUNDLE) if needle in i["raw"])
    ko = rule_ko(item)
    assert ko and expect in ko


def test_status_items_explain_oom_and_zero_endpoints_in_korean():
    from app.interpret import raw_items, rule_ko
    items = raw_items(SAMPLE_BUNDLE)
    oom = next(i for i in items if i["kind"] == "status" and "OOMKilled" in i["raw"])
    assert "메모리" in rule_ko(oom)
    svc = next(i for i in items if i["ref"] == "E3_service")
    assert "엔드포인트" in rule_ko(svc) or "연결 대상" in rule_ko(svc)
    pull = next(i for i in items if i["kind"] == "status" and "ImagePullBackOff" in i["raw"])
    assert "이미지" in rule_ko(pull)


def test_unknown_text_has_no_invented_reading():
    from app.interpret import rule_ko
    assert rule_ko({"kind": "log", "raw": "completely ordinary line", "ref": "E7_error_logs", "source": "loki"}) is None


def test_redaction_is_preserved_in_raw_items():
    from app.interpret import raw_items
    b = {"E7_error_logs": ["password=hunter2 token=abcd1234efgh5678 failed"]}
    raw = raw_items(b)[0]["raw"]
    assert "hunter2" not in raw
