# P8 service-centric map checkpoint

## Scope and decisions

The user clarified that the first screen must show Ingress, services and DB state and failure locations, in an Istio/Kiali-like topology rather than a namespace resource grid. Remaining design choices are delegated to recommended defaults. No new cluster or CNI replacement was performed.

- Layered entry → service call depth → datastore layout.
- Backed Services are folded into workloads, with Service name, ports and ready endpoint count in the detail panel. Unbacked Services remain visible even without traffic.
- System workloads and completed Jobs can be shown with the system checkbox.
- External/unresolved nodes are grouped into one node; the detail panel retains observed peers.
- Configured Ingress routes are dashed; observed network flow events are solid. Flow events are not HTTP request counts. L7 information is unavailable.
- Problem panel focuses a resource; related incident opens the incident workspace. Resource readiness and incident status are shown independently.
- Unclaimed handed-off incidents remain in the incident history but only contribute map incident warnings for six hours after update. Claimed handoffs and resolved/failed incidents do not contribute those warnings. Active processing/approval states have no age filter. This is a display decision, not evidence that an old problem is resolved.

## Observed execution

- Existing context: rancher-desktop.
- Backend: uv run pytest -q, exit 0, entire suite passed.
- Frontend: npm test, 29 passed, 0 failed; npm run build, exit 0.
- Added regression for an unbacked Service with no traffic: failed before the filter fix, passed afterward.
- Backend image build encountered a real dependency network timeout. Deployed an app-only layer on the prior working dependency image; container imports of app.flows and app.main passed. This does not validate a clean rebuild of the full Dockerfile.
- API image pinned to its actual RepoDigest, sha256:3e3f786b07761abd65d8907aacc141304901aa1231f668f956887ce054ae03b2. Initial use of Docker config ID produced ImagePullBackOff; the manifest digest rollout succeeded.
- Dashboard p8j deployed via local dev tag; observed browser bundle index-O-v5xfBD.js.
- Deployment rollback snapshot: .e2e-tmp/revamp-P8-flow/before.yaml.
- Live topology returned entry/service/data/system/job/node/external roles, with connected, non-stale collector. PostgreSQL/pgvector appeared as a datastore.
- Browser selected inventory from problem list, showed inventory Service :8080 with one endpoint and incident #14, then opened /incidents/14.
- Browser selected postgres, showed datastore classification, :5432, one endpoint, 1/1 ready Pod and observed peers including inventory and liveprobe-api.
- System checkbox increased rendered resources from 12 to 28 during observation; these are time-dependent inventory values, not fixed assertions.
- Captures: docs/plans/revamp/P8-service-captures/, four screens at 1440 and 1280, plus selected inventory.

## Still open

P8 service-map verification now includes repository-local Playwright coverage: `npx playwright test` returned 12 passed (9.9s), against the real deployment at both widths. Tests checked Ingress/services/datastore rendering, DB detail, system switch, actual warning-to-incident navigation, no login, route rendering, horizontal overflow and normal/status text token contrast >= 4.5 on white. These contrast checks do not constitute a full accessibility audit. Browser-harness screenshots remain complementary manual evidence.
Design choices use the user's explicit recommendation delegation instead of requesting another design approval. The separate evaluation screen from the original P8 description has not been built; evaluation visualization remains with P10–P12 and is not claimed as delivered here.
P9 is not complete: controlled faults, actual drop/toast/browser notification and newly running agent feed/steering still need end-to-end verification.
P10–P13 remain open: model/pilot verification, spare-account setup, 240-pair run, report/reference guide and new-case validation have not been performed in this increment.
No actual Codex call, new fault injection, external publication or repository commit was performed in this checkpoint.
