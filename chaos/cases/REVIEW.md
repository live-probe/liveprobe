# KAN-70 case catalog — operator-only

These 60 YAML 1.2 files use the JSON subset to avoid an unverified YAML dependency. `python3 chaos/validate.py` checks structure, 15 cases in each Family, unique IDs, allowlisted target/modes, bounded parameters, required labels, and that labels match the approved manifest `LABELS.json`. `python3 -m unittest discover -s chaos/tests -v` runs the tests. `chaos/cases/generate.py` is the reproducible source of these cases.

Family mapping: F1 image/crash, F2 OOM, F3 cascading latency/DB pool, F4 Service/Endpoint loss. `inventory` and `order` both appear in each family. Numbered variants rotate fault modes (where applicable), targets, decoy signals and evidence-sufficiency labels.

## Label status
- label_version 1 is APPROVED (user decision, Jira KAN-90 comment 10021, 2026-09-26): the 60 `ground_truth` blocks and `reference_workflow` labels are the evaluation ground truth as-is.
- Future changes (difficulty tuning) must bump `label_version` and be logged in `CHANGELOG.md`; the validator rejects unrecorded label edits.

## Still open (KAN-71, not covered by the label approval)
- Controlled-fault contract from KAN-47: names/modes, effect of each endpoint, caps, reset, and whether `controlled_endpoint_off` exists. Modes without a confirmed endpoint are specifications, not runnable injection promises.
- Exact baseline for order-targeted faults: deployment readiness, both services' endpoints and `order_check_200` are the planned post-recovery checks.

No case has been injected or recovered. These files must remain outside any LiveProbe container, image build context, ConfigMap, Secret or SA-readable volume (a unit test checks build contexts and `deploy/` manifests). The KAN-71 runner must explicitly map each validated mode to reviewed, bounded operator actions and reject unsupported modes rather than infer shell commands from catalog strings.
