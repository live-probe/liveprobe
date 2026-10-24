"""The rollback verifier must see authoritative Pod image provenance."""
from app.k8s import pod_summary
from app.evidence import compact_summary, Bundle


def pod(spec, status, image_id):
    return {
        "metadata": {"name": "inventory-rs-pod", "labels": {"app": "inventory"},
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "inventory-rs"}]},
        "spec": {"containers": [{"name": "inventory", "image": spec}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "inventory", "image": status, "imageID": image_id,
            "ready": True, "restartCount": 0, "state": {"running": {}}}]},
    }


def test_digest_pinned_spec_with_runtime_tag_alias_exposes_matching_image_id():
    digest = "sha256:" + "a" * 64
    summary = pod_summary(pod("liveprobe/inventory@" + digest, "liveprobe/inventory:kan119",
                              "docker-pullable://liveprobe/inventory@" + digest))
    container = summary["containers"][0]
    assert container["spec_image"] == "liveprobe/inventory@" + digest
    assert container["image"] == "liveprobe/inventory:kan119"
    assert container["image_id"] == "docker-pullable://liveprobe/inventory@" + digest
    assert container["image_provenance"] == "digest_matches_spec"
    assert compact_summary(Bundle("liveprobe-target", "inventory", pods=[summary]))["E2_pods"] == [summary]


def test_mismatched_digest_is_not_asserted_safe():
    summary = pod_summary(pod("liveprobe/inventory@sha256:" + "a" * 64,
                              "liveprobe/inventory:kan119",
                              "docker-pullable://liveprobe/inventory@sha256:" + "b" * 64))
    assert summary["containers"][0]["image_provenance"] == "digest_mismatch"


def test_missing_image_id_remains_unverified():
    summary = pod_summary(pod("liveprobe/inventory@sha256:" + "a" * 64,
                              "liveprobe/inventory:kan119", None))
    assert summary["containers"][0]["image_provenance"] == "unverified"
