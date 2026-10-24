"""Runtime settings (env) + application config (YAML ConfigMap)."""
from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RoleModel(BaseModel):
    model: str
    effort: str | None = "low"


LLM_PROVIDERS = ("codex_cli", "claude_cli", "openai_api", "anthropic_api")


# OpenAI tier lineup (2026-10-18): low = gpt-6-luna, mid = gpt-6.1-sol, high = gpt-6-astra.
# Before 2026-10-18 the mid/high tiers were gpt-5.6-terra / gpt-6-sol (P10-P13 evaluation runs used those).
GPT_LOW, GPT_MID, GPT_HIGH = "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"


def _openai_roles() -> dict[str, RoleModel]:
    return {
        "router": RoleModel(model=GPT_LOW, effort="low"),
        "signal_analyst": RoleModel(model=GPT_LOW, effort="low"),
        "result_verifier": RoleModel(model=GPT_MID, effort="low"),
        "evidence_curator": RoleModel(model=GPT_MID, effort="medium"),
        "safety_verifier": RoleModel(model=GPT_HIGH, effort="xhigh"),
    }


def _anthropic_roles() -> dict[str, RoleModel]:
    # Haiku 4.5 does not take an effort level; Sonnet 5 / Opus 5.5 do (low..max).
    # IDs are the current API aliases listed at platform.claude.com/docs/en/about-claude/models/overview.
    return {
        "router": RoleModel(model="claude-haiku-4-5", effort=None),
        "signal_analyst": RoleModel(model="claude-haiku-4-5", effort=None),
        "result_verifier": RoleModel(model="claude-sonnet-5", effort="low"),
        "evidence_curator": RoleModel(model="claude-sonnet-5", effort="medium"),
        "safety_verifier": RoleModel(model="claude-opus-5-5", effort="high"),
    }


# Standard (short-context, non-batch) list prices, USD per 1M tokens.
# Sources fetched 2026-09-26: https://developers.openai.com/api/docs/pricing and
# https://platform.claude.com/docs/en/about-claude/pricing . Models not listed -> cost unknown (None).
# gpt-6.1-sol / gpt-6-astra fetched 2026-10-18 from developers.openai.com/api/docs/models/<id>.
# For subscription CLIs (codex_cli / claude_cli) this is an API-equivalent estimate, not a bill.
PRICES_VERSION = "2026-10-18"


def _default_prices() -> dict[str, dict[str, float]]:
    return {
        "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
        "gpt-6.1-sol": {"input": 2.00, "cached_input": 0.10, "output": 10.00},
        "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
        # previous lineup, kept so costs of earlier recorded runs can still be estimated
        "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
        "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "output": 10.00},
        "claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00},
        "claude-sonnet-5": {"input": 2.00, "cached_input": 0.20, "output": 10.00},
        "claude-opus-5-5": {"input": 4.00, "cached_input": 0.20, "output": 20.00},
        "claude-opus-5": {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    }


class LLMConfig(BaseModel):
    provider: str = "codex_cli"  # codex_cli | claude_cli | openai_api | anthropic_api
    timeout_s: int = 180
    # Explicit role map used by the engine. When `provider_roles[provider]` is present it replaces
    # `roles`, so switching provider never sends an OpenAI model id to Anthropic (or vice versa).
    roles: dict[str, RoleModel] = Field(default_factory=_openai_roles)
    provider_roles: dict[str, dict[str, RoleModel]] = Field(default_factory=lambda: {
        "codex_cli": _openai_roles(), "openai_api": _openai_roles(),
        "claude_cli": _anthropic_roles(), "anthropic_api": _anthropic_roles(),
    })
    # USD per 1M tokens: {"model": {"input": x, "output": y, "cached_input": z}}. Unknown -> None.
    prices: dict[str, dict[str, float]] = Field(default_factory=_default_prices)
    prices_version: str = PRICES_VERSION

    @model_validator(mode="after")
    def _apply_provider_roles(self) -> "LLMConfig":
        # Precedence: explicit provider_roles[provider] > explicit roles > built-in provider default.
        if self.provider not in LLM_PROVIDERS:
            raise ValueError(f"llm.provider must be one of {LLM_PROVIDERS}, got {self.provider!r}")
        explicit_pr = "provider_roles" in self.model_fields_set and self.provider in self.provider_roles
        if explicit_pr or "roles" not in self.model_fields_set:
            defaults = {"codex_cli": _openai_roles, "openai_api": _openai_roles,
                        "claude_cli": _anthropic_roles, "anthropic_api": _anthropic_roles}
            self.roles = dict(self.provider_roles.get(self.provider) or defaults[self.provider]())
        # Never silently send a model id to the wrong vendor after a provider switch.
        anthropic = self.provider in ("claude_cli", "anthropic_api")
        bad = sorted({rm.model for rm in self.roles.values() if rm.model.startswith("claude") != anthropic})
        if bad:
            raise ValueError(f"llm.roles models {bad} do not belong to provider {self.provider!r}; "
                             "set llm.provider_roles.<provider> or llm.roles for that provider")
        return self


class RouterPolicy(BaseModel):
    # v2 (2026-10-18): log-scale cost term in score_models (see router.py); v1 used a linear term.
    version: str = "v2"
    quality_weight: float = Field(default=0.6, ge=0, le=1)
    cost_weight: float = Field(default=0.25, ge=0, le=1)
    throughput_weight: float = Field(default=0.15, ge=0, le=1)
    # Relative estimates, not benchmark claims. Unknown pricing disqualifies a candidate.
    model_quality: dict[str, float] = Field(default_factory=lambda: {
        GPT_LOW: 0.5, GPT_MID: 0.75, GPT_HIGH: 1.0,
        "claude-haiku-4-5": 0.5, "claude-sonnet-5": 0.75, "claude-opus-5-5": 1.0})
    model_throughput: dict[str, float] = Field(default_factory=lambda: {
        GPT_LOW: 1.0, GPT_MID: 0.6, GPT_HIGH: 0.3,
        "claude-haiku-4-5": 1.0, "claude-sonnet-5": 0.6, "claude-opus-5-5": 0.3})
    min_quality: dict[str, float] = Field(default_factory=lambda: {
        "SIMPLE": 0.5, "NORMAL": 0.7, "COMPLEX": 0.7, "HIGH_RISK": 0.9})

    @model_validator(mode="after")
    def validate_weights(self) -> "RouterPolicy":
        if abs(self.quality_weight + self.cost_weight + self.throughput_weight - 1) > 1e-8:
            raise ValueError("router score weights must total 1")
        if set(self.min_quality) != {"SIMPLE", "NORMAL", "COMPLEX", "HIGH_RISK"}:
            raise ValueError("router min_quality must specify all four levels")
        if any(not 0 <= v <= 1 for scores in (self.model_quality, self.model_throughput, self.min_quality)
               for v in scores.values()):
            raise ValueError("router estimates must be in [0, 1]")
        return self


class ApprovedConfigVersion(BaseModel):
    """An operator-approved, immutable ConfigMap version (KAN-135). Never reconstructed by guessing."""
    name: str                 # versioned ConfigMap name, e.g. inventory-config-v1
    sha256: str               # sha256 of canonical JSON of its .data


class SafeActionPolicy(BaseModel):
    """Bounds for KAN-55/KAN-135 typed actions. Unknown/absent bounds fail closed."""
    # Legacy fields are accepted but never widen the label-based target boundary.
    deployments: list[str] = Field(default_factory=list)
    ownership_labels: dict[str, str] = Field(default_factory=dict)
    managed_label: dict[str, str] = Field(default_factory=lambda: {"liveprobe.io/managed": "true"})
    replicas_min: int = Field(default=1, ge=1)
    replicas_max: int = Field(default=2, ge=1)
    memory_limit_min_mi: int = Field(default=256, ge=16)
    memory_limit_max_mi: int = Field(default=768, ge=16)
    # live ConfigMap name -> approved immutable versions. Empty => configmap_restore always denied.
    configmap_versions: dict[str, list[ApprovedConfigVersion]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounds(self) -> "SafeActionPolicy":
        if not self.managed_label or any(not k.strip() or not v.strip() for k, v in self.managed_label.items()):
            raise ValueError("managed_label must contain non-empty key/value selectors")
        if self.replicas_min > self.replicas_max or self.memory_limit_min_mi > self.memory_limit_max_mi:
            raise ValueError("safe_actions lower bound exceeds upper bound")
        return self


def _group_roles() -> dict[str, dict[str, RoleModel]]:
    """KAN-53 NORMAL/COMPLEX Agents Group role placement (과제계획서 사건 수준별 Group 예시).

    Luna/Terra/Sol tiers per vendor. Used only for roles absent from `llm.roles`, so the
    existing SIMPLE/HIGH_RISK role map (and its provider switch rules) is unchanged.
    """
    # luna/terra/sol are the plan's tier names (low/mid/high); the model ids are the current lineup.
    luna, terra, sol = (RoleModel(model=GPT_LOW, effort="low"), RoleModel(model=GPT_MID, effort="medium"),
                        RoleModel(model=GPT_HIGH, effort="high"))
    haiku, sonnet, opus = (RoleModel(model="claude-haiku-4-5", effort=None),
                           RoleModel(model="claude-sonnet-5", effort="medium"),
                           RoleModel(model="claude-opus-5-5", effort="high"))

    def roles(l: RoleModel, t: RoleModel, s: RoleModel) -> dict[str, RoleModel]:
        return {
            # NORMAL: 진행 조정(Terra/Medium) + K8s 분석(Sol/High) + Log 요약(Luna/Low) + 결과 확인(Terra/Medium)
            "coordinator": t, "k8s_analyst": s, "log_summarizer": l,
            # COMPLEX: K8s/Log 분석(Terra) + 원인 추론(Sol) + 반론 검토(Sol) + 복구 계획(Terra) + 결과 확인(Terra)
            "k8s_log_analyst": t, "cause_reasoner": s, "challenger": s, "recovery_planner": t,
            # independent source verifier shared by both groups
            "group_verifier": t,
        }
    return {"openai": roles(luna, terra, sol), "anthropic": roles(haiku, sonnet, opus)}


class WorkflowPolicy(BaseModel):
    """KAN-53 per-incident limits for the NORMAL/COMPLEX groups. Exceeding any limit hands off.

    Costs are API-list-price estimates (subscription CLIs are not billed per call); a call whose
    price is unknown is refused rather than treated as free.
    """
    version: str = "v2"
    max_agent_calls: dict[str, int] = Field(default_factory=lambda: {"NORMAL": 7, "COMPLEX": 10})
    # v2 (2026-10-18): the high tier became gpt-6-astra (5x the per-token price of the old gpt-6-sol). The caps keep
    # the v1 headroom (~1.3x the projected worst path: base calls + one revise round + one escalation retry);
    # v1 0.60/0.80 would stop a COMPLEX group before its five base calls finish.
    max_cost_usd: dict[str, float] = Field(default_factory=lambda: {"NORMAL": 1.30, "COMPLEX": 2.30})
    deadline_s: int = Field(default=900, ge=30)
    max_rounds: int = Field(default=1, ge=0, le=2)        # extra re-reasoning rounds after a contradiction
    max_escalations_per_role: int = Field(default=1, ge=0, le=2)
    early_stop_confidence: float = Field(default=0.85, ge=0, le=1)
    # Agent-level acceptance floor; below it the agent is escalated. None -> AppConfig.min_confidence.
    min_agent_confidence: float | None = Field(default=None, ge=0, le=1)
    # Pre-call cost projection (worst case); the CLI providers add ~15-16k tokens of own context per call
    # (observed in agent_runs 55-64), so the overhead is part of the projection, not an afterthought.
    projected_output_tokens: int = Field(default=3000, ge=100)
    projected_input_overhead_tokens: int = Field(default=16000, ge=0)
    model_ladder: dict[str, list[str]] = Field(default_factory=lambda: {
        "openai": [GPT_LOW, GPT_MID, GPT_HIGH],
        "anthropic": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5-5"]})
    effort_ladder: list[str] = Field(default_factory=lambda: ["low", "medium", "high", "xhigh"])
    group_roles: dict[str, dict[str, RoleModel]] = Field(default_factory=_group_roles)

    @model_validator(mode="after")
    def _limits(self) -> "WorkflowPolicy":
        for table in (self.max_agent_calls, self.max_cost_usd):
            if set(table) != {"NORMAL", "COMPLEX"} or any(v <= 0 for v in table.values()):
                raise ValueError("workflows limits must be positive for NORMAL and COMPLEX")
        return self


class AssistConfig(BaseModel):
    """P15 helper roles outside the analysis workflow (never part of routing or group budgets).

    interpreter: Korean reading of raw text (display only); steering: classify an operator chat message;
    operator_planner: turn an operator instruction into a Kubernetes change plan. Codex CLI subscription only.
    """
    interpreter: RoleModel = Field(default_factory=lambda: RoleModel(model=GPT_LOW, effort="low"))
    steering: RoleModel = Field(default_factory=lambda: RoleModel(model=GPT_LOW, effort="low"))
    operator_planner: RoleModel = Field(default_factory=lambda: RoleModel(model=GPT_MID, effort="medium"))
    code_fixer: RoleModel = Field(default_factory=lambda: RoleModel(model=GPT_MID, effort="high"))
    interpret_enabled: bool = True
    interpret_max_items: int = Field(default=40, ge=1, le=200)
    interpret_max_chars: int = Field(default=600, ge=80, le=4000)
    command_enabled: bool = True


class AppConfig(BaseModel):
    target_namespaces: list[str] = Field(default_factory=lambda: ["dev", "stg", "prd"], min_length=1)
    # Read-only compatibility alias. Consumers must use membership, not this first environment.
    @property
    def target_namespace(self) -> str:
        return self.target_namespaces[0]

    @model_validator(mode="before")
    @classmethod
    def _legacy_namespace(cls, data):
        if isinstance(data, dict) and "target_namespaces" not in data and data.get("target_namespace"):
            data = {**data, "target_namespaces": [data["target_namespace"]]}
        return data

    @model_validator(mode="after")
    def _unique_namespaces(self):
        if any(not n or not n.strip() for n in self.target_namespaces) or len(set(self.target_namespaces)) != len(self.target_namespaces):
            raise ValueError("target_namespaces must be unique non-empty names")
        return self

    def environment(self, namespace: str | None) -> str | None:
        return namespace if namespace in self.target_namespaces else None

    app_error_poll_interval_seconds: int = Field(default=15, ge=1)
    app_error_threshold: int = Field(default=3, ge=1)
    app_error_window_seconds: int = Field(default=120, ge=1)
    workflows: WorkflowPolicy = Field(default_factory=WorkflowPolicy)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    router: RouterPolicy = Field(default_factory=RouterPolicy)
    assist: AssistConfig = Field(default_factory=AssistConfig)
    runner_systems: dict[str, list[str]] = Field(default_factory=dict)
    change_rollout_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    change_observation_seconds: int = Field(default=60, ge=1, le=600)
    # caller -> list of callees (static dependency map)
    dependencies: dict[str, list[str]] = Field(default_factory=lambda: {"order": ["inventory"]})
    settle_seconds: int = 20
    merge_window_seconds: int = 600
    close_cooldown_seconds: int = 90
    endpoint_poll_interval_seconds: int = 15
    endpoint_grace_seconds: int = 30
    recent_rollout_minutes: int = 15
    rollout_verify_timeout_seconds: int = 120
    min_confidence: float = 0.6
    # Judgement guide (docs/reference, RAG kind reference_guide). Off until the P13 comparison decides; evaluation
    # arms select it explicitly via their rag_kinds.
    use_reference_guide: bool = False
    # Policy C (2026-10-05): the agent may propose these Kubernetes actions; each still passes actions.validate, ownership
    # labels, bounds, the per-level policy and (for the API identity) the executor RBAC/admission policy.
    allowlist: list[str] = Field(default_factory=lambda: ["rollout_undo", "rollout_restart", "pod_delete", "scale_replicas",
                                                          "set_memory_limit"])
    safe_actions: SafeActionPolicy = Field(default_factory=SafeActionPolicy)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVEPROBE_", extra="ignore")

    database_url: str | None = None
    db_host: str = "postgres.liveprobe-system.svc"
    db_port: int = 5432
    db_name: str = "liveprobe"
    db_user: str = "liveprobe"
    db_password: str | None = None

    # Local-only PoC: no user authentication. Requests must carry one of these Host values.
    allowed_hosts: list[str] = ["liveprobe.localhost", "localhost", "127.0.0.1"]
    webhook_token: str | None = None
    hubble_relay: str | None = "hubble-relay.kube-system.svc.cluster.local:80"  # None disables flow collection

    config_file: str = "/etc/liveprobe/config.yaml"
    # projected token of the dedicated write identity (safe-action-executor); absent => typed actions fail closed
    executor_token_file: str = "/var/run/secrets/safe-action-executor/token"
    operator_executor_token_file: str = "/var/run/secrets/operator-executor/token"
    runner_token_file: str = "/var/run/secrets/liveprobe-runner/token"
    release_token_file: str = "/var/run/secrets/release-executor/token"
    secret_writer_token_file: str = "/var/run/secrets/secret-writer/token"
    prometheus_url: str = "http://prometheus.liveprobe-observability.svc:9090"
    loki_url: str = "http://loki.liveprobe-observability.svc:3100"

    background: bool = True  # watcher + worker threads
    kube_in_cluster: bool = True

    codex_bin: str = "codex"
    codex_home: str = "/var/lib/liveprobe/codex"
    # Second (spare) Codex account: its own CODEX_HOME. Unset = single-account mode.
    codex_home_spare: str | None = None
    # Max seconds a run waits for a limited account before it is handed off with the state saved.
    quota_wait_max_s: int = 6 * 3600
    quota_recheck_s: int = 300
    # Policy profile: assessment (default; HIGH_RISK never executes) | poc_autonomy (only on the pinned PoC cluster).
    policy_profile: str = "assessment"
    poc_cluster_uid: str | None = None     # kube-system namespace UID the poc_autonomy profile is pinned to
    openai_api_key: str | None = None
    openai_api_key_file: str | None = "/var/run/secrets/liveprobe-llm/OPENAI_API_KEY"
    # Claude Code CLI (subscription OAuth token from `claude setup-token`) and Anthropic API key.
    # Preferred source is the read-only Secret file (re-read per call, so rotation needs no restart);
    # the env value (same Secret, secretKeyRef) is the fallback. Values are never logged.
    claude_bin: str = "claude"
    claude_oauth_token: str | None = None
    claude_oauth_token_file: str | None = "/var/run/secrets/liveprobe-llm/CLAUDE_CODE_OAUTH_TOKEN"
    anthropic_api_key: str | None = None
    anthropic_api_key_file: str | None = "/var/run/secrets/liveprobe-llm/ANTHROPIC_API_KEY"

    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_cache_dir: str = "/opt/models"
    runbooks_dir: str = str(Path(__file__).resolve().parent.parent / "runbooks")

    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        pw = quote(self.db_password or "", safe="")
        return f"postgresql+psycopg://{self.db_user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"


@functools.lru_cache
def get_settings() -> Settings:
    s = Settings()
    # OPENAI_API_KEY (unprefixed) is the conventional name mounted from the Secret.
    if not s.openai_api_key and os.environ.get("OPENAI_API_KEY"):
        s.openai_api_key = os.environ["OPENAI_API_KEY"]
    if not s.anthropic_api_key and os.environ.get("ANTHROPIC_API_KEY"):
        s.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    if not s.claude_oauth_token and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        s.claude_oauth_token = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    return s


def load_app_config(path: str | None = None) -> AppConfig:
    p = Path(path or get_settings().config_file)
    data: dict[str, Any] = {}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    return AppConfig.model_validate(data)


@functools.lru_cache
def get_app_config() -> AppConfig:
    return load_app_config()
