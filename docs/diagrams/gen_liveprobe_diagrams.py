"""Generate the LiveProbe draw.io pages (mxGraphModel XML) from the actual code structure.

Pages:
  1-A  Workflow 선택 흐름 (signal -> incident -> evidence -> router -> group -> safety)
  1-B  Group 내부 Workflow (SIMPLE / NORMAL / COMPLEX / HIGH_RISK state graphs)
  2    LiveProbe 메가 프로세스
Writes raw page XML files to OUT_DIR; layout is applied afterwards (ELK) by elk_layout.mjs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

# palette (white UI)
C = {
    "start": "fillColor=#E8F1FF;strokeColor=#3B6FD8;fontColor=#12305E;",
    "step": "fillColor=#FFFFFF;strokeColor=#5B6B82;fontColor=#1F2A37;",
    "data": "fillColor=#F4F7FB;strokeColor=#8A9BB3;fontColor=#1F2A37;",
    "dec": "fillColor=#FFF7E0;strokeColor=#C99A1E;fontColor=#4A3A08;",
    "llm": "fillColor=#F1EAFE;strokeColor=#7A55D8;fontColor=#2E1A66;",
    "lvl_s": "fillColor=#E7F6EC;strokeColor=#2F9A55;fontColor=#11402A;",
    "lvl_n": "fillColor=#E6F4F8;strokeColor=#2B8BAA;fontColor=#0E3A49;",
    "lvl_c": "fillColor=#FDEEDC;strokeColor=#D27A1E;fontColor=#4D2A07;",
    "lvl_h": "fillColor=#FCE8E8;strokeColor=#C83A3A;fontColor=#561313;",
    "ok": "fillColor=#E7F6EC;strokeColor=#2F9A55;fontColor=#11402A;",
    "bad": "fillColor=#FCE8E8;strokeColor=#C83A3A;fontColor=#561313;",
    "note": "fillColor=#FFFDF2;strokeColor=#D9CF9E;fontColor=#3D3A2A;align=left;spacingLeft=8;",
    "ext": "fillColor=#EEF1F5;strokeColor=#9AA6B6;fontColor=#2C3440;dashed=1;",
}
BASE = "rounded=1;whiteSpace=wrap;html=1;fontSize=12;arcSize=12;"
DEC = "rhombus;whiteSpace=wrap;html=1;fontSize=11;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;endArrow=block;endFill=1;strokeColor=#5B6B82;"


class Page:
    def __init__(self):
        self.cells: list[str] = []
        self.n = 0

    def _id(self, p: str) -> str:
        self.n += 1
        return f"{p}{self.n}"

    def node(self, nid, label, kind="step", w=200, h=60, shape=None, parent="1", x=0, y=0, extra=""):
        style = (DEC if shape == "dec" else BASE + (shape or "")) + C[kind] + extra
        self.cells.append(
            f'<mxCell id="{nid}" value="{escape(label, {chr(34): "&quot;"})}" style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return nid

    def group(self, gid, title, kind, x, y, w, h):
        style = ("swimlane;startSize=30;html=1;whiteSpace=wrap;rounded=1;arcSize=4;fontSize=13;fontStyle=1;"
                 + C[kind])
        self.cells.append(f'<mxCell id="{gid}" value="{escape(title)}" style="{style}" vertex="1" parent="1">'
                          f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return gid

    def edge(self, s, t, label="", dashed=False, parent="1", color=None):
        style = EDGE + ("dashed=1;" if dashed else "") + (f"strokeColor={color};fontColor={color};" if color else "")
        self.cells.append(f'<mxCell id="{self._id("e")}" value="{escape(label)}" style="{style}" edge="1" '
                          f'source="{s}" target="{t}" parent="{parent}"><mxGeometry relative="1" as="geometry"/></mxCell>')

    def xml(self) -> str:
        return ('<mxGraphModel grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" '
                'page="1" pageScale="1" pageWidth="1654" pageHeight="1169" background="#FFFFFF" math="0" shadow="0">'
                '<root><mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(self.cells) + "</root></mxGraphModel>")


def b(t: str) -> str:
    return f"<b>{t}</b>"


# ----------------------------------------------------------------------------------------------- page 1-A
def page_1a() -> str:
    p = Page()
    p.node("sig", b("이상 신호") + "<br>Pod·Event 감시 / Endpoint 폴링<br>Alertmanager 경보(OOM·지연·풀·5xx)",
           "start", 240, 70)
    p.node("inc", b("사건화 (incidents.ingest)") + "<br>같은 지문 10분 병합 · 종료 후 90초 재발 억제<br>"
           "평가 잠금 중에는 무시 · 20초 대기 후 분석", "step", 270, 70)
    p.node("evi", b("근거 수집 E1~E7 (읽기 전용)") + "<br>Deployment·리비전 / Pod / Service·Endpoint<br>"
           "Event / Prometheus 지표 / Loki 로그 / 호출자", "data", 280, 80)
    p.node("d_ev", "핵심 근거<br>(deployment·pod·<br>service·endpoint)<br>수집 실패?", "dec", 170, 110, "dec")
    p.node("h_fail", b("운영자 이관") + "<br>router_failure<br>(자동 조치 없음)", "bad", 180, 64)
    p.node("d_hr", "보호 자원(Node·Secret·NS)<br>또는 Selector 0<br>또는 경로 장애?", "dec", 200, 120, "dec")
    p.node("d_s", "이미지/크래시 오류<br>+ 15분 내 롤아웃<br>+ 단일 workload?", "dec", 200, 120, "dec")
    p.node("d_n", "OOM 또는<br>지연·연결 풀 신호?", "dec", 180, 110, "dec")
    p.node("l_h", b("HIGH_RISK") + " (규칙 확정)", "lvl_h", 170, 44)
    p.node("l_s", b("SIMPLE"), "lvl_s", 140, 44)
    p.node("l_n", b("NORMAL"), "lvl_n", 140, 44)
    p.node("l_c", b("COMPLEX") + "<br>(규칙에 안 맞음)", "lvl_c", 150, 50)
    p.node("llm", b("LLM 분류기") + " (router: gpt-6-luna/low)<br>수준을 <u>올리기만</u> 가능 · 최대 COMPLEX<br>"
           "HIGH_RISK는 규칙만 선언 · 낮추자는 의견은 무시", "llm", 300, 76)
    p.node("d_llm", "분류 실패 또는<br>신뢰도 &lt; 0.6?", "dec", 170, 100, "dec")
    p.node("score", b("주 분석 모델 선택 (score_models v2)") + "<br>품질 0.6 + 비용 0.25(로그 척도) + 처리량 0.15<br>"
           "수준별 최소 품질 미달·가격 미상 모델 제외", "llm", 320, 76)
    p.node("g_s", b("simple_group") + "<br>주 모델 gpt-6-luna<br>Agent 2 (분석·확인)", "lvl_s", 180, 70)
    p.node("g_n", b("normal_group") + "<br>주 모델 gpt-6.1-sol<br>Agent 4 (병렬 분석·조정·검증)", "lvl_n", 200, 70)
    p.node("g_c", b("complex_group") + "<br>주 모델 gpt-6.1-sol<br>Agent 5 (원인 추론·반론·복구 계획)", "lvl_c", 220, 70)
    p.node("g_h", b("high_risk_group") + "<br>근거 정리 + 안전 검증<br>(gpt-6-astra/xhigh)", "lvl_h", 200, 70)
    p.node("d_act", "독립 검증 통과 +<br>제안자·검증자가<br>같은 조치?", "dec", 180, 120, "dec")
    p.node("pol", b("정책 판정") + "<br>SIMPLE·NORMAL·COMPLEX = 자동<br>전역 approval / suggest_only / 일시정지 우선<br>"
           "실행 직전 사건 수준 재확인", "step", 290, 80)
    p.node("exe", b("안전 실행기 (별도 쓰기 계정)") + "<br>허용 조치 6종 · 복제 1..2 · 메모리 256..768Mi<br>"
           "before 스냅샷 · 동시성 검사 · admission 정책", "step", 300, 80)
    p.node("d_ver", "실행 후<br>검증 통과?", "dec", 150, 100, "dec")
    p.node("res", b("해결 (resolved)"), "ok", 160, 50)
    p.node("comp", b("보상 실행(원복)") + "<br>→ 운영자 이관", "bad", 170, 56)
    p.node("h_end", b("운영자 이관 (handed_off)") + "<br>원인 후보 ≤3·근거·권장 절차·반론 첨부", "bad", 260, 64)

    p.edge("sig", "inc")
    p.edge("inc", "evi", "open → analyzing")
    p.edge("evi", "d_ev")
    p.edge("d_ev", "h_fail", "예")
    p.edge("d_ev", "d_hr", "아니오")
    p.edge("d_hr", "l_h", "예")
    p.edge("d_hr", "d_s", "아니오")
    p.edge("d_s", "l_s", "예")
    p.edge("d_s", "d_n", "아니오")
    p.edge("d_n", "l_n", "예")
    p.edge("d_n", "l_c", "아니오")
    for lv in ("l_h", "l_s", "l_n", "l_c"):
        p.edge(lv, "llm")
    p.edge("llm", "d_llm")
    p.edge("d_llm", "h_fail", "예")
    p.edge("d_llm", "score", "아니오")
    p.edge("score", "g_s", "SIMPLE")
    p.edge("score", "g_n", "NORMAL")
    p.edge("score", "g_c", "COMPLEX")
    p.edge("score", "g_h", "HIGH_RISK")
    for g in ("g_s", "g_n", "g_c"):
        p.edge(g, "d_act")
    p.edge("g_h", "h_end", "자동 조치 중지")
    p.edge("d_act", "pol", "예")
    p.edge("d_act", "h_end", "아니오")
    p.edge("pol", "exe", "auto")
    p.edge("pol", "h_end", "approval·suggest_only")
    p.edge("exe", "d_ver")
    p.edge("d_ver", "res", "예")
    p.edge("d_ver", "comp", "아니오")
    p.edge("comp", "h_end")
    return p.xml()


# ----------------------------------------------------------------------------------------------- page 1-B
def page_1b() -> str:
    p = Page()
    # SIMPLE
    p.node("s1", b("signal_analyst") + "<br>gpt-6-luna / low<br>원인 + 조치 1개 제안", "lvl_s", 200, 64)
    p.node("s2", b("result_verifier") + "<br>gpt-6.1-sol / low<br>독립 확인", "lvl_s", 200, 64)
    p.node("s3", "두 Agent가<br>같은 조치 +<br>사전검사 통과?", "dec", 170, 110, "dec")
    p.node("s_ok", b("조치 제안") + "<br>(SIMPLE)", "ok", 150, 50)
    p.node("s_no", b("이관"), "bad", 120, 44)
    p.edge("s1", "s2"); p.edge("s2", "s3"); p.edge("s3", "s_ok", "예"); p.edge("s3", "s_no", "아니오")
    # NORMAL
    p.node("n1", b("k8s_analyst") + "<br>gpt-6-astra / high<br>객체 상태 분석·조치 제안", "lvl_n", 210, 64)
    p.node("n2", b("log_summarizer") + "<br>gpt-6-luna / low<br>로그·지표·호출자 요약", "lvl_n", 210, 64)
    p.node("n3", "주 분석 근거 확인 +<br>신뢰도 ≥ 0.85 +<br>더 강한 로그 가설 없음?", "dec", 200, 120, "dec")
    p.node("n4", b("coordinator") + "<br>gpt-6.1-sol / medium<br>원인 후보 ≤3 조정·조치 제안", "lvl_n", 220, 64)
    p.node("n5", b("group_verifier") + "<br>gpt-6.1-sol / medium<br>원본 근거 재확인·조치 승인", "lvl_n", 220, 64)
    p.node("n6", "반박됨 +<br>재검토 1회 미만?", "dec", 170, 100, "dec")
    p.node("n_fin", b("finalize") + " (NORMAL)", "step", 170, 44)
    p.edge("n1", "n3", "병렬"); p.edge("n2", "n3", "병렬")
    p.edge("n3", "n5", "예 · 조기 종료(조정 생략)"); p.edge("n3", "n4", "아니오")
    p.edge("n4", "n5"); p.edge("n5", "n6")
    p.edge("n6", "n4", "예 · coordinator 승격 재검토", color="#C83A3A"); p.edge("n6", "n_fin", "아니오")
    # COMPLEX
    p.node("c1", b("k8s_log_analyst") + "<br>gpt-6.1-sol / medium<br>K8s·로그·지표 정리", "lvl_c", 210, 64)
    p.node("c2", b("cause_reasoner") + "<br>gpt-6-astra / high<br>원인 ≤3 추론 사슬", "lvl_c", 210, 64)
    p.node("c3", b("challenger") + "<br>gpt-6-astra / high<br>반증·대안 설명", "lvl_c", 200, 64)
    p.node("c4", b("recovery_planner") + "<br>gpt-6.1-sol / medium<br>되돌릴 수 있는 복구 계획·조치", "lvl_c", 220, 64)
    p.node("c5", "반론이 1순위<br>원인을 반박?", "dec", 160, 100, "dec")
    p.node("c6", b("revise") + "<br>cause_reasoner 승격 재추론", "lvl_c", 200, 54, extra="dashed=1;")
    p.node("c7", b("group_verifier") + "<br>gpt-6.1-sol / medium", "lvl_c", 200, 54)
    p.node("c8", "반박됨 +<br>재검토 1회 미만?", "dec", 170, 100, "dec")
    p.node("c_fin", b("finalize") + " (COMPLEX)", "step", 170, 44)
    p.edge("c1", "c2"); p.edge("c2", "c3", "병렬"); p.edge("c2", "c4", "병렬")
    p.edge("c3", "c5"); p.edge("c4", "c5")
    p.edge("c5", "c6", "예", color="#C83A3A"); p.edge("c5", "c7", "아니오 · 조기 종료")
    p.edge("c6", "c7"); p.edge("c7", "c8")
    p.edge("c8", "c6", "예", color="#C83A3A"); p.edge("c8", "c_fin", "아니오")
    # HIGH_RISK
    p.node("h1", b("evidence_curator") + "<br>gpt-6.1-sol / medium<br>근거 정리", "lvl_h", 200, 64)
    p.node("h2", b("safety_verifier") + "<br>gpt-6-astra / xhigh<br>안전 검증", "lvl_h", 200, 64)
    p.node("h3", b("자동 조치 중지") + "<br>운영자 이관", "bad", 170, 54)
    p.edge("h1", "h2"); p.edge("h2", "h3")
    # finalize (shared)
    p.node("fin", b("finalize 공통") + "<br>근거 확인된 후보만 ≤3 · 독립 검증 supported 필요<br>"
           "검증자가 <u>같은 조치</u>를 승인하고 허용 목록·이전 리비전·대상 확인 통과 → 조치 제안<br>"
           "그 외 → 원인 후보·반론·권장 절차를 붙여 이관", "note", 430, 90)
    p.edge("n_fin", "fin"); p.edge("c_fin", "fin")
    # common rules (not connected)
    p.node("rule1", b("모든 Agent 공통 — 선택적 승격") + "<br>출력 오류 · 인용 위조 · 없는 근거 번호 · 근거 없는 원인 · 신뢰도 &lt; 0.6<br>"
           "→ <u>그 Agent만</u> 1회 승격: 모델 luna → 6.1-sol → astra, 최상위면 추론 low → medium → high → xhigh<br>"
           "시간초과·인증 오류는 승격하지 않음(근거 문제가 아니므로)", "note", 520, 90)
    p.node("rule2", b("예산 게이트 (매 호출 전)") + "<br>호출 수 NORMAL 7 · COMPLEX 10<br>"
           "추정 비용 NORMAL $1.30 · COMPLEX $2.30 · 제한 시간 900초<br>넘으면 그 자리에서 중단 → 이관", "note", 430, 84)
    return p.xml()


# ----------------------------------------------------------------------------------------------- page 2
def page_2() -> str:
    p = Page()
    W, H = 300, 150
    procs = [
        ("p1", "1. 감시·사건화", "start",
         "Pod·Event watch, Endpoint 폴링<br>Alertmanager 경보 수신(웹훅)<br>Hubble 흐름 → 지도 네트워크 경고<br>"
         "지문 병합·재발 억제·평가 잠금"),
        ("p2", "2. 근거 수집·정리", "data",
         "K8s 상태·리비전·Event (읽기 전용)<br>Prometheus 지표·Loki 로그(사건 시점 기준)<br>"
         "E1~E7 요약 · 비밀값 가림<br>수집 실패도 근거로 남김"),
        ("p3", "3. 수준 판단·Workflow·모델 선택", "llm",
         "규칙 + LLM 분류(올리기만)<br>SIMPLE·NORMAL·COMPLEX·HIGH_RISK<br>품질·비용·처리량 점수로 주 모델 선택<br>"
         "선택 이유·점수 저장"),
        ("p4", "4. Agents Group 분석·검증", "lvl_n",
         "LangGraph 상태 그래프(순차·병렬·분기)<br>역할별 luna / 6.1-sol / astra 혼합<br>"
         "인용 검사·선택적 승격·조기 종료<br>예산 게이트·독립 검증"),
        ("p5", "5. 안전 조치·검증·원복", "ok",
         "허용 조치 6종(롤백·재시작·파드 삭제·복제·메모리·설정)<br>정책·한도·admission 정책<br>"
         "before 스냅샷 → 실행 → 검증 → 보상<br>롤백은 전용 롤백 컨트롤러"),
        ("p6", "6. 운영자 이관·개입", "bad",
         "이관 패키지(원인 후보·근거·반론·절차)<br>인수(claim)·승인/거절<br>실행 중 지시 메시지·일시정지·중단"),
        ("p7", "7. 기록·학습", "step",
         "사건·근거·라우팅·Agent 호출·조치·감사 로그<br>실행 기록 스트림(SSE)·검토 장부<br>"
         "종료 사건 → RAG 과거 사건 색인"),
        ("p8", "8. 운영 대시보드", "step",
         "첫 화면 Kiali(실제 메시)<br>서비스 지도·사건 작업 화면<br>검토 장부·설정(자동화 모드)"),
        ("p9", "9. 평가 (오프라인)", "ext",
         "장애 사례 60 + 새 사례 12 주입·복구<br>4방식 비교(Single-small/High, Fixed, Adaptive)<br>"
         "지표 엔진·정답 유출 검사·결과보고서"),
    ]
    # hand-placed grid: row 1 = detect -> decide, row 2 = act / hand off, row 3 = record / evaluate
    pos = {"p1": (40, 160), "p2": (420, 160), "p3": (800, 160), "p4": (1180, 160),
           "p8": (40, 460), "p6": (610, 460), "p5": (1180, 460),
           "p9": (40, 760), "p7": (610, 760)}
    for pid, title, kind, body in procs:
        x, y = pos[pid]
        p.node(pid, f"<b style='font-size:14px'>{title}</b><hr size='1'>{body}", kind, W, H, x=x, y=y,
               extra="align=left;verticalAlign=top;spacingLeft=10;spacingTop=6;")
    ext = [("hub", "Retina / Hubble<br>(네트워크 흐름)", 90, 30), ("k8s", "Kubernetes API<br>(관찰 계정 / 쓰기 계정 분리)", 470, 30),
           ("obs", "Prometheus · Loki<br>Alertmanager", 850, 30), ("llmx", "Codex CLI (구독)<br>gpt-6-luna · 6.1-sol · astra", 1230, 30),
           ("db", "PostgreSQL + pgvector<br>(사건·감사·RAG)", 1230, 805)]
    for eid, label, x, y in ext:
        p.node(eid, label, "ext", 200, 60, shape="", x=x, y=y)
    p.edge("p1", "p2", "사건 생성")
    p.edge("p2", "p3", "근거 묶음")
    p.edge("p3", "p4", "수준·Group·모델")
    p.edge("p4", "p5", "검증된 조치 제안")
    p.edge("p4", "p6", "저신뢰·고위험·미해결", color="#C83A3A")
    p.edge("p5", "p6", "검증 실패·승인 대기", color="#C83A3A")
    p.edge("p5", "p7", "결과")
    p.edge("p6", "p7", "운영자 결정")
    p.edge("p7", "p4", "과거 사건·Runbook 검색", dashed=True)
    p.edge("p8", "p6", "운영자 조작", dashed=True)
    p.edge("p7", "p8", "실시간 기록", dashed=True)
    p.edge("p9", "p1", "장애 주입", dashed=True)
    p.edge("k8s", "p1", dashed=True); p.edge("obs", "p1", dashed=True); p.edge("hub", "p8", dashed=True)
    p.edge("k8s", "p2", dashed=True); p.edge("obs", "p2", dashed=True)
    p.edge("llmx", "p4", dashed=True); p.edge("p5", "k8s", "제한된 쓰기", dashed=True)
    p.edge("p7", "db", dashed=True)
    return p.xml()


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, fn in (("p1a", page_1a), ("p1b", page_1b), ("p2", page_2)):
        (OUT_DIR / f"{name}.xml").write_text(fn(), encoding="utf-8")
        print("wrote", OUT_DIR / f"{name}.xml")
