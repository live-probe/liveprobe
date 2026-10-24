"""P15: Korean names and one-line job descriptions for every agent role (display only).

Shown in the incident war room so an operator sees what each agent does instead of a bare role id.
"""
from __future__ import annotations

ROLES: dict[str, dict] = {
    "router": {"role_ko": "사건 수준 판단", "job_ko": "근거를 보고 사건을 단순·보통·복잡·고위험으로 나누고 맡길 Agent Group을 고릅니다.",
               "group": None},
    "signal_analyst": {"role_ko": "신호 분석", "job_ko": "이벤트·Pod 상태·로그에서 장애 신호를 읽고 원인 후보와 조치를 제안합니다.", "group": "SIMPLE"},
    "result_verifier": {"role_ko": "결과 확인", "job_ko": "신호 분석 결론이 원본 근거와 맞는지 독립적으로 다시 확인합니다.", "group": "SIMPLE"},
    "coordinator": {"role_ko": "진행 조정", "job_ko": "Kubernetes 분석과 로그 요약을 합쳐 원인 후보와 복구 조치를 정리합니다.", "group": "NORMAL"},
    "k8s_analyst": {"role_ko": "Kubernetes 분석", "job_ko": "Deployment·Pod·Service·이벤트·노드 상태를 읽어 원인을 좁힙니다.", "group": "NORMAL"},
    "log_summarizer": {"role_ko": "로그 요약", "job_ko": "호출자 상태·지표·오류 로그를 요약해 증상을 정리합니다.", "group": "NORMAL"},
    "group_verifier": {"role_ko": "결과 확인", "job_ko": "그룹의 결론과 인용이 원본 근거와 일치하는지 독립적으로 검증합니다.", "group": None},
    "k8s_log_analyst": {"role_ko": "Kubernetes·로그 분석", "job_ko": "모든 근거를 함께 읽고 사건의 예비 원인 후보를 만듭니다.", "group": "COMPLEX"},
    "cause_reasoner": {"role_ko": "원인 추론", "job_ko": "예비 후보와 근거를 비교해 가장 그럴듯한 원인과 복구 조치를 추론합니다.", "group": "COMPLEX"},
    "challenger": {"role_ko": "반론 검토", "job_ko": "원인 추론에 반대되는 근거를 찾아 결론의 약점을 지적합니다.", "group": "COMPLEX"},
    "recovery_planner": {"role_ko": "복구 계획", "job_ko": "허용된 Kubernetes 조치 범위 안에서 복구 순서와 위험을 계획합니다.", "group": "COMPLEX"},
    "evidence_curator": {"role_ko": "근거 정리", "job_ko": "고위험 사건의 근거를 정리하고 운영자가 확인할 내용을 추립니다.", "group": "HIGH_RISK"},
    "safety_verifier": {"role_ko": "안전 검증", "job_ko": "자동 조치를 멈춰야 하는지와 운영자에게 넘길 내용을 검증합니다.", "group": "HIGH_RISK"},
    "interpreter": {"role_ko": "한글 해석", "job_ko": "영어 원문 로그·이벤트·Agent 결론을 한국어로 풀어 씁니다. 분석 판단에는 쓰지 않습니다.", "group": None},
    "steering": {"role_ko": "지시 해석", "job_ko": "운영자의 채팅을 읽고 어떤 단계에 어떻게 반영할지 정합니다.", "group": None},
    "operator_planner": {"role_ko": "작업 계획", "job_ko": "운영자 지시를 Kubernetes 변경 계획으로 바꾸고 부작용을 설명합니다.", "group": None},
}

GROUP_KO = {"SIMPLE": "단순 Group", "NORMAL": "보통 Group", "COMPLEX": "복잡 Group", "HIGH_RISK": "고위험 Group"}
LEVEL_KO = {"SIMPLE": "단순", "NORMAL": "보통", "COMPLEX": "복잡", "HIGH_RISK": "고위험"}


def role_info(role: str) -> dict:
    info = ROLES.get(role)
    if info is None:
        return {"role": role, "role_ko": role, "job_ko": "등록되지 않은 역할입니다.", "group": None}
    return {"role": role, **info}
