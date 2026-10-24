"""Page 1-B: the four group state graphs, each drawn in its own frame and laid out per frame.

Each group is laid out separately with the draw.io MCP ELK pass (so nodes of different groups never interleave),
then the four laid-out groups are placed side by side as titled containers.
"""
from __future__ import annotations

import json
import subprocess
import sys
import xml.dom.minidom as minidom
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_liveprobe_diagrams import C, Page, b  # noqa: E402

HERE = Path(__file__).resolve().parent


def simple(p: Page):
    p.node("s1", b("signal_analyst") + "<br>gpt-6-luna / low<br>원인 + 조치 1개 제안", "lvl_s", 200, 64)
    p.node("s2", b("result_verifier") + "<br>gpt-6.1-sol / low<br>독립 확인", "lvl_s", 200, 64)
    p.node("s3", "두 Agent가<br>같은 조치 +<br>사전검사 통과?", "dec", 170, 110, "dec")
    p.node("s_ok", b("조치 제안") + "<br>→ 안전 실행기", "ok", 150, 50)
    p.node("s_no", b("운영자 이관"), "bad", 130, 44)
    p.edge("s1", "s2"); p.edge("s2", "s3"); p.edge("s3", "s_ok", "예"); p.edge("s3", "s_no", "아니오")


def normal(p: Page):
    p.node("n1", b("k8s_analyst") + "<br>gpt-6-astra / high<br>객체 상태 분석·조치 제안", "lvl_n", 210, 64)
    p.node("n2", b("log_summarizer") + "<br>gpt-6-luna / low<br>로그·지표·호출자 요약", "lvl_n", 210, 64)
    p.node("n3", "주 분석 근거 확인 +<br>신뢰도 ≥ 0.85 +<br>더 강한 로그 가설 없음?", "dec", 210, 120, "dec")
    p.node("n4", b("coordinator") + "<br>gpt-6.1-sol / medium<br>원인 후보 ≤3 조정·조치 제안", "lvl_n", 220, 64)
    p.node("n5", b("group_verifier") + "<br>gpt-6.1-sol / medium<br>원본 근거 재확인·조치 승인", "lvl_n", 220, 64)
    p.node("n6", "반박됨 +<br>재검토 1회 미만?", "dec", 170, 100, "dec")
    p.node("n_fin", b("finalize"), "step", 150, 44)
    p.edge("n1", "n3", "병렬"); p.edge("n2", "n3", "병렬")
    p.edge("n3", "n4", "아니오"); p.edge("n3", "n5", "예 · 조정 생략")
    p.edge("n4", "n5"); p.edge("n5", "n6")
    p.edge("n6", "n4", "예 · 승격 재검토", color="#C83A3A"); p.edge("n6", "n_fin", "아니오")


def complex_(p: Page):
    p.node("c1", b("k8s_log_analyst") + "<br>gpt-6.1-sol / medium<br>K8s·로그·지표 정리", "lvl_c", 210, 64)
    p.node("c2", b("cause_reasoner") + "<br>gpt-6-astra / high<br>원인 ≤3 추론 사슬", "lvl_c", 210, 64)
    p.node("c3", b("challenger") + "<br>gpt-6-astra / high<br>반증·대안 설명", "lvl_c", 200, 64)
    p.node("c4", b("recovery_planner") + "<br>gpt-6.1-sol / medium<br>복구 계획·조치 제안", "lvl_c", 200, 64)
    p.node("c5", "반론이 1순위<br>원인을 반박?", "dec", 160, 100, "dec")
    p.node("c6", b("revise") + "<br>cause_reasoner 승격 재추론", "lvl_c", 210, 54, extra="dashed=1;")
    p.node("c7", b("group_verifier") + "<br>gpt-6.1-sol / medium<br>원본 근거 재확인·조치 승인", "lvl_c", 220, 64)
    p.node("c8", "반박됨 +<br>재검토 1회 미만?", "dec", 170, 100, "dec")
    p.node("c_fin", b("finalize"), "step", 150, 44)
    p.edge("c1", "c2"); p.edge("c2", "c3", "병렬"); p.edge("c2", "c4", "병렬")
    p.edge("c3", "c5"); p.edge("c4", "c5")
    p.edge("c5", "c6", "예", color="#C83A3A"); p.edge("c5", "c7", "아니오 · 재추론 생략")
    p.edge("c6", "c7"); p.edge("c7", "c8")
    p.edge("c8", "c6", "예", color="#C83A3A"); p.edge("c8", "c_fin", "아니오")


def high_risk(p: Page):
    p.node("h1", b("evidence_curator") + "<br>gpt-6.1-sol / medium<br>근거 정리", "lvl_h", 200, 64)
    p.node("h2", b("safety_verifier") + "<br>gpt-6-astra / xhigh<br>안전 검증", "lvl_h", 200, 64)
    p.node("h3", b("자동 조치 중지") + "<br>운영자 이관", "bad", 170, 54)
    p.edge("h1", "h2"); p.edge("h2", "h3")


GROUPS = [
    ("g_s", "SIMPLE — simple_group (Agent 2)", "lvl_s", simple),
    ("g_n", "NORMAL — normal_group (Agent 4, 병렬 시작)", "lvl_n", normal),
    ("g_c", "COMPLEX — complex_group (Agent 5, 반론 병렬)", "lvl_c", complex_),
    ("g_h", "HIGH_RISK — high_risk_group", "lvl_h", high_risk),
]


def layout(fragment_xml: str, work: Path, name: str, pkg: str) -> str:
    raw, out = work / f"{name}.raw.xml", work / f"{name}.xml"
    raw.write_text(fragment_xml, encoding="utf-8")
    subprocess.run(["node", str(HERE / "elk_layout.mjs"), pkg, str(raw), str(out), "vertical"], check=True,
                   capture_output=True)
    return out.read_text(encoding="utf-8")


def build(work: Path, pkg: str) -> str:
    work.mkdir(parents=True, exist_ok=True)
    page = Page()
    x, top, gap, title_h, pad = 20, 240, 40, 34, 20
    for gid, title, kind, fn in GROUPS:
        sub = Page()
        fn(sub)
        laid = minidom.parseString(layout(sub.xml(), work, gid, pkg))
        cells = [c for c in laid.getElementsByTagName("mxCell") if c.getAttribute("id") not in ("0", "1")]
        geo = {c.getAttribute("id"): c.getElementsByTagName("mxGeometry")[0] for c in cells if c.getAttribute("vertex")}
        minx = min(float(g.getAttribute("x") or 0) for g in geo.values())
        miny = min(float(g.getAttribute("y") or 0) for g in geo.values())
        maxx = max(float(g.getAttribute("x") or 0) + float(g.getAttribute("width")) for g in geo.values())
        maxy = max(float(g.getAttribute("y") or 0) + float(g.getAttribute("height")) for g in geo.values())
        w, h = maxx - minx + 2 * pad, maxy - miny + 2 * pad + title_h
        page.group(gid, title, kind, x, top, w, h)
        for c in cells:
            c.setAttribute("parent", gid)
            g = c.getElementsByTagName("mxGeometry")[0]
            if c.getAttribute("vertex"):
                g.setAttribute("x", str(float(g.getAttribute("x") or 0) - minx + pad))
                g.setAttribute("y", str(float(g.getAttribute("y") or 0) - miny + pad + title_h))
            for pt in g.getElementsByTagName("mxPoint"):
                parent_tag = getattr(pt.parentNode, "tagName", "")
                if pt.getAttribute("as") in ("sourcePoint", "targetPoint") or parent_tag == "Array":
                    pt.setAttribute("x", str(float(pt.getAttribute("x") or 0) - minx + pad))
                    pt.setAttribute("y", str(float(pt.getAttribute("y") or 0) - miny + pad + title_h))
            page.cells.append(c.toxml())
        x += w + gap
    # notes across the top
    page.node("rule1", b("모든 Agent 공통 — 선택적 승격") + "<br>출력 오류 · 인용 위조 · 없는 근거 번호 · 근거 없는 원인 · "
              "신뢰도 &lt; 0.6 → <u>그 Agent만</u> 1회 승격<br>모델 luna → 6.1-sol → astra, 이미 최상위면 추론 강도 "
              "low → medium → high → xhigh<br>시간초과·인증 오류는 승격하지 않음(근거 문제가 아니므로)",
              "note", 560, 96, x=20, y=20)
    page.node("rule2", b("예산 게이트 (매 호출 전, NORMAL·COMPLEX)") + "<br>호출 수 NORMAL 7 · COMPLEX 10<br>"
              "추정 비용 NORMAL $1.30 · COMPLEX $2.30 · 제한 시간 900초<br>넘으면 그 자리에서 중단 → 이관",
              "note", 440, 96, x=600, y=20)
    page.node("rule3", b("finalize (NORMAL·COMPLEX 공통)") + "<br>근거가 확인된 원인 후보만 최대 3개 · 독립 검증 supported 필요<br>"
              "검증자가 <u>같은 조치</u>를 승인 + 허용 목록·이전 리비전·대상 확인 통과 → 조치 제안<br>"
              "그 외 → 원인 후보·반론·권장 절차를 붙여 운영자 이관", "note", 560, 96, x=1060, y=20)
    page.node("legend", b("모델 등급") + "<br>낮음 gpt-6-luna · 중간 gpt-6.1-sol · 최고 gpt-6-astra<br>"
              "빨간 선 = 반박 시 재검토 경로(최대 1회)", "note", 380, 96, x=1640, y=20)
    return page.xml()


if __name__ == "__main__":
    work, pkg = Path(sys.argv[1]), sys.argv[2]
    (work / "p1b.xml").write_text(build(work / "p1b-parts", pkg), encoding="utf-8")
    print(json.dumps({"wrote": str(work / "p1b.xml")}))
