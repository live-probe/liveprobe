#!/usr/bin/env python3
"""Verify the actual OCI index/configs of a local buildx artifact; do not trust build output text."""
import json
import sys
import tarfile

path, app = sys.argv[1:3]
with tarfile.open(path) as tar:
    def blob(digest):
        member = tar.getmember("blobs/sha256/" + digest.split(":", 1)[1])
        file = tar.extractfile(member)
        assert file is not None
        return json.load(file)

    index_file = tar.extractfile("index.json")
    assert index_file is not None
    index = json.load(index_file)
    architectures = set()
    def inspect(descriptors):
        for item in descriptors:
            document = blob(item["digest"])
            if "manifests" in document:
                inspect(document["manifests"])
            elif "config" in document:
                config = blob(document["config"]["digest"])
                if config.get("os") == "linux":
                    architectures.add(config.get("architecture"))
    inspect(index["manifests"])
    expected = {"amd64", "arm64"}
    assert expected <= architectures, f"{app}: missing {expected - architectures}; found {architectures}"
    print(f"PASS {app}: OCI index configs = linux/amd64 + linux/arm64 ({path})")
