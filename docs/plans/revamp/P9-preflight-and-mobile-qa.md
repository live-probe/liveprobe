# Served UI verification and P9 preflight

## Target lineage

Repository: /Users/hwany/sds-ai-professional, main, uncommitted working tree.
Served dashboard: local image liveprobe/dashboard:p8mobile2, RepoDigest sha256:187c54582b19ef9f4f846951d21a6d38d6265f658ba96f79997a8ae3fc7aa066.
Observed JS: index-DQ4mxE0y.js; CSS: index-Djva7K8w.css.
No repository commit or external publication was made.

## Desktop/mobile observations

Viewport checks: 1440x900, 1280x900 and 390x844.
Initial 390px capture revealed overlapping map controls/problem list and an inaccessible selected-resource sidebar. These were real failures, not passing evidence.
Fixes: responsive shell sizing and wrapped status bar; distinct map-control/problem-list positions; dropdown selection with centered resource; mobile bottom-sheet detail panel with a close button.
Observed final mobile bounds: controls y=104..211.5, problem list y=246..339.5, both x=64..382 within a 390px viewport. No overlap.
Actual inventory warning opened the bottom sheet; Service endpoint and Pod status text wrapped/readable. Close removed the detail panel. Actual warning -> incident #14 navigation passed on mobile and desktop.
Desktop map shows entry/service/datastore layers and a problem badge; mobile topology is pannable rather than fitting the entire graph into unreadably small nodes, and every resource is selectable from the dropdown.
Incident mobile title and badges wrap, pause/stop/send controls are visible; full detail context remains a desktop-only panel, and the historical-event message mentions a right panel not visible on mobile (remaining copy/detail-access limitation).
Settings mobile policy explanatory text wraps and the page scrolls; no policy was saved during QA.
Four routes were captured at desktop/mobile in docs/plans/revamp/P8-service-captures/*-latest-{1440,390}.png, plus selected-inventory-390.png and map-390.png.

## Execution receipts

- npm test: 29 passed, 0 failed.
- npm run build: exit 0.
- Final npm run test:e2e: 18 passed (7.6s), exit 0, one worker, 390/1280/1440 widths.
- Earlier mobile browser regression runs failed (initially 2 failures, then 1 initial-load failure). The overlap was fixed; initial-load assertions now wait for the actual resource selector before checking role nodes. No tests were skipped or mocked.
- Tests cover live role nodes, DB selection/detail visibility, system switch, actual warning-to-incident route, route headings, absence of login, horizontal page overflow and text/status token contrast on white.
- Manual browser session showed delayed API requests after many route navigations (resource entries around 49 seconds); a hard reload returned settled settings. curl independently returned healthy endpoints. Cause is not established; no claim of universal network/performance health.

## Visual gate boundary

Responsive interaction checks above passed. Full OMH visual QA verdict is HOLD, not an overall PASS: independent dual review, keyboard/focus audit, objective pixel comparison and motion coverage are not observed. Remaining limitations include mobile historical incident context access/copy and the unexplained long browser request delay. Rendered evidence is not a complete accessibility or performance audit.

## P9 advanced: observed read-only preflight

- Reviewed scripts/e2e-mvp0.sh: it injects two real faults and expects real Codex agent runs, citations and executed action evidence; it is not a harmless UI smoke test.
- Existing rancher-desktop target inventory/order/loadgen Deployments each ready 1/1.
- Actual loadgen -> order /api/orders/check?item=a returned HTTP 200.
- Policy profile endpoint: assessment, no downgrade.
- Accounts endpoint: primary available, single mode, spare not configured.
- API readiness returned status=ok, db=true, background=true.

P9 is blocked on the prior explicit user instruction pausing real application LLM calls. Faults that trigger Codex calls have not been injected. Request explicit resumption before running the fault-to-agent/action workflow. P10–P13 remain dependent and incomplete; two-account evaluation also needs the spare login. No newly running agent feed, actual network-drop notification, high-risk execution or 240-pair evaluation is claimed here.
