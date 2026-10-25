"""Versioned, Codex-only role placement. Never edits Kubernetes policy or active-run configs."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from .config import AppConfig, GPT_LOW, GPT_MID, GPT_HIGH, RoleModel
from .db import audit
from .models import Setting

KEY = 'group_model_settings'
GROUP_ROLES = {
    'SIMPLE': ('signal_analyst', 'result_verifier'),
    'NORMAL': ('coordinator', 'k8s_analyst', 'log_summarizer', 'group_verifier'),
    'COMPLEX': ('k8s_log_analyst', 'cause_reasoner', 'challenger', 'recovery_planner', 'group_verifier'),
    'HIGH_RISK': ('evidence_curator', 'safety_verifier'),
}
PRIMARY = {'SIMPLE': 'signal_analyst', 'NORMAL': 'k8s_analyst', 'COMPLEX': 'cause_reasoner',
           'HIGH_RISK': 'evidence_curator'}
MODELS = (GPT_LOW, GPT_MID, GPT_HIGH)
EFFORTS = ('low', 'medium', 'high', 'xhigh')


class Placement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: Literal['gpt-6-luna', 'gpt-6.1-sol', 'gpt-6-astra']
    effort: Literal['low', 'medium', 'high', 'xhigh']


class GroupPlacement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    primary_mode: Literal['router', 'fixed']
    roles: dict[str, Placement]


class ModelSettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    router: Placement
    groups: dict[str, GroupPlacement]
    assist: dict[str, Placement] | None = None

    @model_validator(mode='after')
    def complete(self):
        if set(self.groups) != set(GROUP_ROLES):
            raise ValueError('네 가지 그룹을 모두 지정해야 합니다.')
        for level, roles in GROUP_ROLES.items():
            if set(self.groups[level].roles) != set(roles):
                raise ValueError(f'{level} 역할 구성이 올바르지 않습니다.')
        if self.assist is not None and set(self.assist) != {'code_fixer'}:
            raise ValueError('코드 수정 역할을 지정해야 합니다.')
        return self


class RevisionConflict(ValueError):
    pass


def defaults(cfg: AppConfig) -> dict:
    def role(name):
        rm = cfg.llm.roles.get(name) or cfg.workflows.group_roles['openai'][name]
        return rm.model_dump()
    return {'assist': {'code_fixer': cfg.assist.code_fixer.model_dump()}, 'router': role('router'), 'groups': {
        level: {'primary_mode': 'router', 'roles': {name: role(name) for name in roles}}
        for level, roles in GROUP_ROLES.items()}}


def read_settings(s, cfg: AppConfig) -> dict:
    row = s.get(Setting, KEY)
    base = defaults(cfg)
    value = row.value if row else {'revision': 0, **base}
    return {'revision': value['revision'], 'source': 'database' if row else 'config',
            'provider': cfg.llm.provider, 'editable': cfg.llm.provider == 'codex_cli',
            'updated_at': row.updated_at.isoformat() if row and row.updated_at else None,
            'router': value['router'], 'groups': value['groups'], 'assist': value.get('assist', base['assist']), 'defaults': base,
            'catalog': [{'model': name, 'efforts': EFFORTS, 'price': cfg.llm.prices.get(name)} for name in MODELS],
            'prices_version': cfg.llm.prices_version,
            'limits': {'max_cost_usd': cfg.workflows.max_cost_usd, 'max_agent_calls': cfg.workflows.max_agent_calls,
                       'model_ladder': cfg.workflows.model_ladder['openai'], 'effort_ladder': cfg.workflows.effort_ladder}}


def validate_settings(cfg: AppConfig, body: ModelSettingsIn) -> None:
    if cfg.llm.provider != 'codex_cli':
        raise ValueError('이 화면은 Codex CLI 전용입니다. 호출 공급자는 변경할 수 없습니다.')
    # Validate the common classifier too, not only group roles.
    import math
    placements = [body.router, *(rm for group in body.groups.values() for rm in group.roles.values()), *(body.assist or {}).values()]
    for rm in placements:
        price = cfg.llm.prices.get(rm.model) or {}
        if (not {'input', 'output'} <= set(price)
                or any(not math.isfinite(price[key]) or price[key] < 0 for key in ('input', 'output'))):
            raise ValueError(f'{rm.model}의 가격 정보가 없습니다.')
        if rm.model not in (cfg.workflows.model_ladder.get('openai') or []):
            raise ValueError(f'{rm.model}이 등록된 승격 순서에 없습니다.')
    # The UI cannot weaken independent verification or the primary-role quality floor.
    for level, group in body.groups.items():
        for role, rm in group.roles.items():
            if role in ('result_verifier', 'group_verifier') and rm.model == GPT_LOW:
                raise ValueError('결과 검증 역할은 중간 모델 이상을 사용해야 합니다.')
            if role in ('challenger', 'safety_verifier') and rm.model != GPT_HIGH:
                raise ValueError('반론·안전 검증 역할은 최고 모델을 사용해야 합니다.')
            if role == 'safety_verifier' and rm.effort not in ('high', 'xhigh'):
                raise ValueError('안전 검증은 high 또는 xhigh 추론이 필요합니다.')
        primary = group.roles[PRIMARY[level]]
        if group.primary_mode == 'fixed' and (cfg.router.model_quality.get(primary.model, 0)
                                              < cfg.router.min_quality[level]):
            raise ValueError(f'{level} 주 모델의 최소 품질을 충족하지 않습니다.')


def save_settings(s, cfg: AppConfig, body: ModelSettingsIn, actor: str) -> dict:
    validate_settings(cfg, body)
    # Covers even the first insert: SELECT FOR UPDATE alone cannot lock an absent row.
    s.execute(text('SELECT pg_advisory_xact_lock(48950)'))
    current = read_settings(s, cfg)
    if current['revision'] != body.expected_revision:
        raise RevisionConflict('다른 창에서 설정을 변경했습니다. 다시 불러온 뒤 저장하세요.')
    value = {'revision': current['revision'] + 1, 'router': body.router.model_dump(),
             'groups': {k: v.model_dump() for k, v in body.groups.items()},
             'assist': {k: v.model_dump() for k, v in body.assist.items()} if body.assist is not None else current['assist']}
    row = s.get(Setting, KEY)
    if row:
        row.value = value
    else:
        s.add(Setting(key=KEY, value=value))
    audit(s, actor, 'group_model_settings_changed', revision=value['revision'],
          previous_revision=current['revision'], configuration={'router': value['router'], 'groups': value['groups']})
    s.flush()
    return read_settings(s, cfg)


def snapshot_config(s, cfg: AppConfig) -> tuple[AppConfig, dict | None]:
    """Take a deep, run-local copy once. Evaluation callers deliberately bypass this function."""
    frozen = cfg.model_copy(deep=True)
    row = s.get(Setting, KEY)
    if cfg.llm.provider != 'codex_cli':
        return frozen, None
    if row is None:
        # Config defaults are a real version too: preserve their complete run placement.
        return frozen, {'revision': 0, **defaults(frozen)}
    value = row.value
    # Corrupt storage must fail before any model call; never silently adopt partial settings.
    parsed = ModelSettingsIn.model_validate({'expected_revision': value['revision'],
                                             'router': value['router'], 'groups': value['groups']})
    validate_settings(frozen, parsed)
    snapshot = {'revision': value['revision'], 'router': parsed.router.model_dump(),
                'groups': {k: v.model_dump() for k, v in parsed.groups.items()}}
    frozen.llm.roles['router'] = RoleModel(**snapshot['router'])
    return frozen, snapshot


def apply_group(cfg: AppConfig, snapshot: dict | None, level: str, policy: dict | None) -> tuple[AppConfig, dict]:
    """Resolve one group's role map without leaking a shared role into another group."""
    out = dict(policy or {})
    if snapshot is None:
        return cfg, out
    active = cfg.model_copy(deep=True)
    group = snapshot['groups'][level]
    active.llm.roles.update({role: RoleModel(**rm) for role, rm in group['roles'].items()})
    out['router_selected_model'] = out.get('selected_model')
    if group['primary_mode'] == 'fixed':
        out['selected_model'] = group['roles'][PRIMARY[level]]['model']
    roles = {role: dict(rm) for role, rm in group['roles'].items()}
    if out.get('selected_model'):
        roles[PRIMARY[level]]['model'] = out['selected_model']
    out['model_settings'] = {'revision': snapshot['revision'], 'primary_mode': group['primary_mode'],
                             'router': snapshot['router'], 'roles': roles}
    return active, out
