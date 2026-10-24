#!/usr/bin/env python3
"""Safely add Claude OAuth from a private assignment file to the existing LLM Secret.

Usage: python3 scripts/update-claude-secret.py PATH BACKUP_DIR
Never sources the file, prints credential material, or removes existing Secret keys.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


KEY = b"CLAUDE_CODE_OAUTH_TOKEN"


def parse_assignment(raw: bytes) -> bytes:
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0].startswith(KEY + b"="):
        raise ValueError("expected exactly one CLAUDE_CODE_OAUTH_TOKEN assignment")
    value = lines[0][len(KEY) + 1:]
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in (b"'", b'"'):
        value = value[1:-1]
    if not value or any(c in value for c in (b"\r", b"\n", b"\x00", b"'", b'"', b" ", b"\t")):
        raise ValueError("invalid token format")
    return value


def run(args: list[str], input_data: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=input_data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"kubectl failed with exit status {result.returncode}")
    return result.stdout


def update(token_file: Path, backup_dir: Path) -> tuple[str, list[str]]:
    info = token_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("token file must be owner-owned regular file with mode 0600 or stricter")
    token = parse_assignment(token_file.read_bytes())
    context = ["kubectl", "--context", "rancher-desktop", "-n", "liveprobe-system"]
    raw = run(context + ["get", "secret", "liveprobe-llm", "-o", "json"])
    secret = json.loads(raw)
    keys = sorted(secret["data"])
    if "auth.json" not in keys:
        raise ValueError("existing Codex auth.json key missing; refusing mutation")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)
    backup = backup_dir / "liveprobe-llm.json"
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    digest = hashlib.sha256(raw).hexdigest()
    replacement = {"apiVersion": "v1", "kind": "Secret", "metadata": {
        "name": "liveprobe-llm", "namespace": "liveprobe-system",
        "resourceVersion": secret["metadata"]["resourceVersion"]},
        "type": secret.get("type", "Opaque"), "data": dict(secret["data"])}
    replacement["data"][KEY.decode()] = base64.b64encode(token).decode("ascii")
    run(context + ["replace", "-f", "-"], json.dumps(replacement).encode())
    after = json.loads(run(context + ["get", "secret", "liveprobe-llm", "-o", "json"]))
    if not set(keys).issubset(after["data"]) or after["data"]["auth.json"] != secret["data"]["auth.json"]:
        raise RuntimeError("post-update key preservation check failed; use restricted backup to restore")
    return digest, sorted(after["data"])


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("usage: update-claude-secret.py TOKEN_ENV_FILE NEW_BACKUP_DIR")
        digest, keys = update(Path(sys.argv[1]), Path(sys.argv[2]))
        print(f"Secret updated; original backup sha256={digest}; keys={','.join(keys)}")
    except (ValueError, OSError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"Secret update failed: {type(exc).__name__}; inspect prerequisites or restricted backup", file=sys.stderr)
        sys.exit(1)
