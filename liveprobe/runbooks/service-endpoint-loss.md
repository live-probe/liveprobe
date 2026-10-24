---
title: Service endpoint loss (selector mismatch)
source: internal-runbook
applies_to: [no_endpoints, selector_mismatch]
risk: high
---

# Service endpoint loss (selector mismatch)

## Symptoms
A Service has zero ready endpoints (`kubectl get endpoints <svc>` shows `<none>`)
even though the backing pods are Running and Ready. Callers of the Service fail with
connection errors and return 5xx. The Deployment itself looks healthy.

## Typical root causes
- The Service `spec.selector` was changed and no longer matches the pod labels
  (for example selector `app=inventory-broken` while pods are labeled `app=inventory`).
- Pod labels were changed in a new Deployment revision without updating the Service.
- All pods failing readiness probes.

## Diagnosis
1. Compare the Service selector with the labels of the pods that should back it.
2. Check whether pods are Ready; if they are, the problem is the selector/labels, not the pods.
3. Identify every caller affected by the missing endpoints (blast radius across services).

## Remediation
This is a HIGH-RISK change: the Service selector controls traffic routing and may be shared by
several consumers or managed by GitOps. Automatic changes are not allowed. Hand off to an operator with:
- the current selector and the matching pod labels,
- the list of affected callers,
- the recommended fix: restore the selector from the source-of-truth manifest
  (for example `kubectl apply -k` of the declarative configuration), then verify endpoints > 0
  and caller 5xx rate returns to zero.
Rolling back the Deployment does NOT fix a Service selector mismatch.
