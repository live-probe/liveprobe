"""Evidence-grounded mitigation/code-fix classification, independent of an LLM."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class CodeRef(BaseModel):
    file: str
    line: int = Field(ge=1)
    symbol: str


class Repro(BaseModel):
    method: str
    path: str
    query: str


class Remediation(BaseModel):
    kind: Literal['mitigation', 'code_fix', 'both', 'none']
    why: str
    code_refs: list[CodeRef]
    repro: Repro | None


REMEDIATION_SCHEMA = Remediation.model_json_schema()
# Inline definitions for all structured-output providers.
for name, definition in list(REMEDIATION_SCHEMA.pop('$defs', {}).items()):
    def inline(node):
        if isinstance(node, dict):
            if node.get('$ref') == '#/$defs/' + name:
                node.clear()
                node.update(definition)
            for value in list(node.values()): inline(value)
        elif isinstance(node, list):
            for value in node: inline(value)
    inline(REMEDIATION_SCHEMA)

REMEDIATION_RULES = (
    'Include remediation {kind: mitigation|code_fix|both|none, why, code_refs:[{file,line,symbol}], '
    'repro:{method,path,query}|null}. Distinguish immediate operational mitigation from a permanent code fix. '
    'Use E8_code_refs user-code frames verbatim; never invent paths, line numbers or a reproduction request. '
    'Use both for an exception after a recent image change (rollback mitigates, code repair remains). '
    'Independent verifiers must compare remediation and every code_ref/repro against raw evidence, not just repeat the analyst.'
)


def remediation_hint(bundle, *, recent_minutes: int = 15, now: datetime | None = None) -> dict:
    from .k8s import parse_ts
    refs = [{k: f[k] for k in ('file', 'line', 'symbol')} for f in (bundle.code_refs or {}).get('frames', [])
            if f.get('file') and type(f.get('line')) is int and f['line'] > 0 and 'symbol' in f]
    dep = bundle.deployment or {}
    created = parse_ts(dep.get('recent_rollout_at') or dep.get('current_revision_created'))
    now = now or datetime.now(timezone.utc)
    recent = created is not None and 0 <= (now - created).total_seconds() <= recent_minutes * 60
    changed = any(r.get('revision') != dep.get('current_revision') and r.get('images') != dep.get('images')
                  for r in dep.get('revisions') or [])
    if refs:
        kind = 'both' if recent and changed else 'code_fix'
        why = '최근 이미지 변경과 사용자 코드 예외가 함께 관측됨: 롤백 완화와 코드 수정 필요' if kind == 'both' else '사용자 코드 예외 위치 관측: 코드 수정 필요, 최근 이미지 변경 원인은 확인되지 않음'
    elif bundle.pods or bundle.service or bundle.events or bundle.metrics or bundle.deployment:
        kind, why = 'mitigation', '자원·워크로드·지연 신호에 대한 운영 완화 우선, 사용자 코드 예외 위치 미관측'
    else:
        kind, why = 'none', '판정 근거 미관측'
    return {'kind': kind, 'why': why, 'code_refs': refs[:40], 'repro': None, 'source': 'rules'}


def remediation_errors(value: dict | None, evidence: dict) -> list[str]:
    if value is None:
        return []  # Older outputs are handled by the deterministic fallback.
    try:
        parsed = Remediation.model_validate(value).model_dump()
    except (ValidationError, TypeError):
        return ['invalid remediation schema']
    observed = (evidence.get('E8_code_refs') or {}).get('frames') or []
    errors = []
    for ref in parsed['code_refs']:
        if not any(all(frame.get(k) == v for k, v in ref.items()) for frame in observed):
            errors.append('remediation code_ref not found in E8_code_refs')
    if parsed['kind'] in ('code_fix', 'both') and not parsed['code_refs']:
        errors.append('code fix requires observed user-code references')
    if parsed['repro'] is not None and parsed['repro'] != (evidence.get('E8_code_refs') or {}).get('repro'):
        errors.append('reproduction request not observed in evidence')
    return errors


def select_remediation(bundle, output=None, *, verified: bool = False) -> dict:
    value = (output or {}).get('remediation')
    if verified and value and not remediation_errors(value, {'E8_code_refs': bundle.code_refs}):
        return {**Remediation.model_validate(value).model_dump(), 'source': 'llm'}
    return remediation_hint(bundle)


def remediation_label(value: dict | None) -> str | None:
    if not value:
        return None
    label = {'code_fix': '코드 수정 필요', 'both': '운영 완화와 코드 수정 필요', 'mitigation': '운영 완화 우선', 'none': '판정 근거 부족'}.get(value.get('kind'))
    refs = value.get('code_refs') or []
    return f"{label} — {refs[0]['file'].rsplit('/', 1)[-1]}:{refs[0]['line']}" if label and refs else label
