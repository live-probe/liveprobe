---
title: Image pull failures and CrashLoopBackOff after a rollout
source: internal-runbook
applies_to: [ImagePullBackOff, ErrImagePull, CrashLoopBackOff]
risk: low
---

# Image pull failures and CrashLoopBackOff after a rollout

## Symptoms
A Deployment's newest ReplicaSet has pods stuck in `ErrImagePull` or `ImagePullBackOff`,
or containers restart repeatedly in `CrashLoopBackOff`. Events show `Failed to pull image`,
`manifest unknown`, `not found`, or `Back-off pulling image`. The rollout does not complete:
`updatedReplicas` is lower than `replicas` or `availableReplicas` drops.

## Typical root causes
- A typo or non-existent image tag was set during the latest rollout (for example `:does-not-exist`).
- The registry is unreachable or credentials (imagePullSecret) are wrong.
- For CrashLoopBackOff: the new version fails on startup (bad configuration, missing dependency).

## Diagnosis
1. Compare the image in the current pod template with the image of the previous ReplicaSet revision.
2. Check whether the failure started right after the revision changed (recent rollout).
3. Confirm only the one workload is affected; other workloads using the same registry are healthy.
4. Check that older pods of the previous revision are still Running (rolling update protects capacity).

## Remediation
If the failure began with the latest rollout and a previous healthy revision exists,
the safe, reversible remediation is `rollout undo` to the previous revision
(`kubectl rollout undo deployment/<name>`). This restores the last known-good pod template.
After the undo verify: rollout complete, `updatedReplicas == replicas == availableReplicas`,
no pods in a waiting state. Then fix the image tag in the source of truth (Git/manifest) before re-deploying.

## Do not
- Do not delete pods repeatedly; they will pull the same bad image.
- Do not edit the image by hand to a guessed tag.
