"""P18 HTTP trust boundaries: runner Bearer and dashboard guard are independent."""
from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import require_user
from .changes import ChangeError, ChangeService
from .config import get_settings


class ReleaseKube:
    """Release-only projected identity, never an observer/operator credential fallback."""
    def __init__(self, token_file):
        from .operator_kube import bound_api_client
        if not Path(token_file).is_file(): raise ChangeError('release_executor_unavailable', 503)
        self.api_client = bound_api_client(token_file)

    def patch_image(self, namespace, service, previous, new, resource_version):
        from .commands import safe_name
        if namespace not in ('dev', 'stg', 'prd') or not safe_name(service): raise ChangeError('invalid_release_target', 422)
        path = f'/apis/apps/v1/namespaces/{namespace}/deployments/{service}'
        options = {'auth_settings': ['BearerToken'], 'response_type': 'object', '_return_http_data_only': True,
                   '_request_timeout': (3, 8)}
        try:
            deployment = self.api_client.call_api(path, 'GET', **options)
            containers = deployment['spec']['template']['spec']['containers']
            indexes = [i for i, c in enumerate(containers) if c.get('name') == service]
            if len(indexes) != 1: raise ChangeError('invalid_release_container')
            idx = indexes[0]
            if deployment['metadata']['resourceVersion'] != resource_version or containers[idx]['image'] != previous:
                raise ChangeError('stale_plan')
            image_path = f'/spec/template/spec/containers/{idx}/image'
            body = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': resource_version},
                    {'op': 'test', 'path': image_path, 'value': previous},
                    {'op': 'replace', 'path': image_path, 'value': new}]
            return self.api_client.call_api(path, 'PATCH', body=body,
                header_params={'Content-Type': 'application/json-patch+json'}, **options)
        except ChangeError: raise
        except Exception as exc:
            raise ChangeError('release_write_conflict' if getattr(exc, 'status', None) == 409 else 'release_write_failed',
                              409 if getattr(exc, 'status', None) == 409 else 502) from None

    def close(self): self.api_client.close()


def setup_changes(engine, state):
    release = None
    try: release = ReleaseKube(engine.settings.release_token_file)
    except Exception: pass  # No identity fallback. Planning/building still work without release permissions.
    service = ChangeService(engine.cfg, engine.settings, engine.provider, engine.kube, release=release)
    service.recover()
    engine.changes = service; state['changes'] = service


class PromotionIn(BaseModel):
    stage: Literal['dev', 'stg', 'prd']
    plan_sha256: str = Field(min_length=64, max_length=64)
    confirm: Literal[True]


def change_router(state, *, settings=None):
    router = APIRouter()

    def backend():
        service = state.get('changes')
        if service is None: raise HTTPException(503, 'change_service_unavailable')
        return service

    def runner_auth(request: Request):
        cfg = settings or get_settings()
        try: expected = Path(cfg.runner_token_file).read_bytes().strip()
        except OSError: raise HTTPException(503, 'runner_api_unavailable') from None
        if not expected: raise HTTPException(503, 'runner_api_unavailable')
        supplied = request.headers.get('authorization', '')
        valid = supplied.startswith('Bearer ') and hmac.compare_digest(supplied[7:].encode(), expected)
        if not valid: raise HTTPException(401, 'runner_authentication_required')

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 3 * 1024 * 1024: raise HTTPException(413, 'runner_body_too_large')
        try:
            out = json.loads(raw) if raw else {}
            if not isinstance(out, dict): raise ValueError()
            return out
        except (ValueError, UnicodeError): raise HTTPException(422, 'invalid_runner_payload') from None
        finally: raw.clear()

    def invoke(method, *args):
        try: return getattr(backend(), method)(*args)
        except ChangeError as exc: raise HTTPException(exc.status_code, exc.code) from None

    @router.post('/api/runner/claim', dependencies=[Depends(runner_auth)])
    async def claim(request: Request):
        payload = await body(request)
        job = await run_in_threadpool(invoke, 'claim', payload.get('systems'))
        return {'job': job} if job else Response(status_code=204)

    @router.post('/api/runner/jobs/{jid}/result', dependencies=[Depends(runner_auth)])
    async def report(jid: int, request: Request):
        return await run_in_threadpool(invoke, 'report', jid, await body(request))

    @router.get('/api/runner/changes', dependencies=[Depends(runner_auth)])
    def runner_changes():
        return {'items': invoke('list', None)}

    @router.post('/api/runner/changes/{cid}/review', dependencies=[Depends(runner_auth)])
    async def review(cid: int, request: Request):
        return await run_in_threadpool(invoke, 'review', cid, await body(request))

    @router.get('/api/changes')
    def listing(incident_id: int | None = None, user: str = Depends(require_user)):
        return {'items': invoke('list', incident_id)}

    @router.post('/api/incidents/{iid}/changes', status_code=201)
    def create(iid: int, user: str = Depends(require_user)):
        return invoke('create_for_incident', iid, user)

    @router.get('/api/changes/{cid}')
    def detail(cid: int, user: str = Depends(require_user)):
        return invoke('get', cid)

    @router.get('/api/changes/{cid}/plan')
    def plan(cid: int, stage: Literal['dev', 'stg', 'prd'], user: str = Depends(require_user)):
        return invoke('plan', cid, stage)

    @router.post('/api/changes/{cid}/promote')
    def promote(cid: int, data: PromotionIn, tasks: BackgroundTasks, user: str = Depends(require_user)):
        row = invoke('promote', cid, data.stage, data.plan_sha256, data.confirm, user)
        tasks.add_task(backend().execute_stage, cid, data.stage)
        return row

    @router.post('/api/changes/{cid}/cancel')
    def cancel(cid: int, user: str = Depends(require_user)):
        return invoke('cancel', cid, user)

    return router
