"""KAN-66 alert identity contract: only named target alerts become LiveProbe signals."""
from types import SimpleNamespace

import pytest

from app.main import ALERT_KIND, alert_signal
from conftest import family1_kube


@pytest.mark.parametrize("name,labels,expected", [
    ("TargetWorkloadImagePullFailing", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetWorkloadCrashLooping", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetWorkloadOOMKilled", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetServiceNoEndpoints", {"endpoint": "inventory"}, ("Service", "inventory")),
    ("OrderHigh5xxRate", {"app": "order"}, ("App", "order")),
    ("InventoryHigh5xxRate", {"app": "inventory"}, ("App", "inventory")),
    ("TargetHighLatency", {"app": "inventory"}, ("App", "inventory")),
    ("TargetConnectionPoolExhausted", {"app": "inventory", "pool": "inventory"}, ("App", "inventory")),
])
def test_each_alert_maps_to_expected_workload(name, labels, expected):
    assert name in ALERT_KIND
    signal = alert_signal({"labels": {"alertname": name, "namespace": "liveprobe-target", **labels}},
                          SimpleNamespace(target_namespace="liveprobe-target"), family1_kube())
    assert signal is not None
    assert (signal.kind, signal.name) == expected
    assert signal.reason == name


@pytest.mark.parametrize("labels", [
    {"alertname": "TargetHighLatency", "namespace": "kube-system", "app": "inventory"},
    {"alertname": "UnrecognizedAlert", "namespace": "liveprobe-target", "app": "inventory"},
    {"alertname": "TargetWorkloadOOMKilled", "namespace": "liveprobe-target", "pod": "missing"},
])
def test_unrecognized_or_unresolvable_alert_ignored(labels):
    assert alert_signal({"labels": labels}, SimpleNamespace(target_namespace="liveprobe-target"),
                        family1_kube()) is None
