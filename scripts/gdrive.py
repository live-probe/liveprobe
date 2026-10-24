#!/usr/bin/env python3
"""Minimal Google Drive helper for the diagram workspace.

Credentials:
  - OAuth client: <repo>/google_oauth.json (git-ignored; never printed)
  - User token:   ~/.hermes/google_drive_token.json (chmod 600, outside the repo)

Scope is Drive only (https://www.googleapis.com/auth/drive): the workspace is an
existing Drive file, which the narrower drive.file scope cannot open.

Usage:
  gdrive.py auth [--port 8765]        # opens a local callback; approve in the browser
  gdrive.py check
  gdrive.py get FILE_ID               # metadata (JSON)
  gdrive.py download FILE_ID OUT
  gdrive.py upload PATH [--parent FOLDER_ID] [--name NAME]
  gdrive.py update FILE_ID PATH       # replace the content of an existing file
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_FILE = Path(__file__).resolve().parent.parent / "google_oauth.json"
TOKEN_FILE = Path.home() / ".hermes" / "google_drive_token.json"
DRAWIO_MIME = "application/vnd.jgraph.mxfile"
FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,parents,owners(emailAddress),capabilities(canEdit)"


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        sys.exit("NOT_AUTHENTICATED: run `gdrive.py auth` first")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save(creds)
        else:
            sys.exit("REFRESH_FAILED: run `gdrive.py auth` again")
    return creds


def _save(creds) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)


def _svc():
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def _mime(path: Path) -> str:
    if path.suffix == ".drawio":
        return DRAWIO_MIME
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def cmd_auth(a) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_FILE.exists():
        sys.exit(f"missing OAuth client file: {CLIENT_FILE}")
    data = json.loads(CLIENT_FILE.read_text())
    kind = "installed" if "installed" in data else "web" if "web" in data else None
    if kind is None:
        sys.exit("not a Google OAuth client file (needs 'installed' or 'web')")
    flow = InstalledAppFlow.from_client_config(data, SCOPES)
    print(f"client type: {kind}; redirect: http://localhost:{a.port}/", flush=True)
    creds = flow.run_local_server(port=a.port, open_browser=not a.no_browser, access_type="offline",
                                  prompt="consent", authorization_prompt_message="AUTH_URL: {url}",
                                  success_message="LiveProbe Drive 인증이 끝났습니다. 이 창을 닫아도 됩니다.",
                                  timeout_seconds=a.timeout)
    if creds is None:
        sys.exit("AUTH_TIMEOUT")
    _save(creds)
    print("AUTHENTICATED")


def cmd_check(_a) -> None:
    about = _svc().about().get(fields="user(emailAddress)").execute()
    print("AUTHENTICATED as", about["user"]["emailAddress"])


def cmd_get(a) -> None:
    print(json.dumps(_svc().files().get(fileId=a.file_id, fields=FIELDS, supportsAllDrives=True).execute(),
                     ensure_ascii=False, indent=2))


def cmd_download(a) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    svc = _svc()
    meta = svc.files().get(fileId=a.file_id, fields="name,mimeType", supportsAllDrives=True).execute()
    if meta["mimeType"].startswith("application/vnd.google-apps."):
        sys.exit(f"Google-native file ({meta['mimeType']}); export is not supported here")
    req = svc.files().get_media(fileId=a.file_id, supportsAllDrives=True)
    with open(a.out, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    print(json.dumps({"status": "downloaded", "name": meta["name"], "mimeType": meta["mimeType"], "path": a.out},
                     ensure_ascii=False))


def cmd_upload(a) -> None:
    from googleapiclient.http import MediaFileUpload

    p = Path(a.path)
    body = {"name": a.name or p.name, "mimeType": _mime(p)}
    if a.parent:
        body["parents"] = [a.parent]
    f = _svc().files().create(body=body, media_body=MediaFileUpload(str(p), mimetype=_mime(p)),
                              fields=FIELDS, supportsAllDrives=True).execute()
    print(json.dumps({"status": "uploaded", **f}, ensure_ascii=False, indent=2))


def cmd_update(a) -> None:
    from googleapiclient.http import MediaFileUpload

    p = Path(a.path)
    f = _svc().files().update(fileId=a.file_id, media_body=MediaFileUpload(str(p), mimetype=_mime(p)),
                              fields=FIELDS, supportsAllDrives=True).execute()
    print(json.dumps({"status": "updated", **f}, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("auth")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_auth)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    s = sub.add_parser("get")
    s.add_argument("file_id")
    s.set_defaults(fn=cmd_get)
    s = sub.add_parser("download")
    s.add_argument("file_id")
    s.add_argument("out")
    s.set_defaults(fn=cmd_download)
    s = sub.add_parser("upload")
    s.add_argument("path")
    s.add_argument("--parent")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_upload)
    s = sub.add_parser("update")
    s.add_argument("file_id")
    s.add_argument("path")
    s.set_defaults(fn=cmd_update)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
