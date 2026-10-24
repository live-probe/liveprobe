# Ground-truth label changelog (operator-only)

Labels = each case's `reference_workflow` + `ground_truth`. `LABELS.json` pins a sha256 per case;
`python3 chaos/validate.py` fails if any label changes without a new `label_version`.

To change labels (e.g. difficulty tuning):
1. Edit `generate.py` (preferred) or the case YAML, regenerate.
2. `python3 chaos/validate.py --write-labels <N+1> --approved-on YYYY-MM-DD --approval-ref "<Jira ref>"`
   (use `--status draft` until the user approves).
3. Add an entry below listing changed case IDs, old -> new values and the approving Jira reference.
4. Evaluation results must record the `label_version` they were scored against.

## v1 — 2026-09-26 — approved
- All 60 cases (F1-01..F4-15) approved as-is by the user; Jira KAN-90 comment 10021.
- The user plans to tune difficulty/classification later; record every such change as v2+.
- Approval covers labels only. No case has been injected/recovered yet (KAN-71).
