---
title: Cascading 5xx errors between services
source: internal-runbook
applies_to: [http_5xx, upstream_failure]
risk: high
---

# Cascading 5xx errors between services

## Symptoms
A caller service (for example `order`) returns HTTP 5xx (500/502/503/504) at an elevated rate.
Its logs show errors calling a downstream dependency (for example `inventory`):
connection refused, timeouts, `UnknownHostException`, or `inventory unavailable`.

## Typical root causes
- The downstream service has no ready endpoints (selector mismatch, all pods unready).
- The downstream service is slow (latency cascade, thread pool exhaustion).
- Network policy or DNS change blocking the call.

## Diagnosis
1. Identify the failing downstream dependency from the caller's error logs.
2. Check the downstream Service endpoints and pod readiness.
3. Check if the caller itself changed recently; usually the caller is a victim, not the cause.
4. Correlate the start of the 5xx rate with changes on the downstream side.

## Remediation
Fix the downstream component. Restarting or rolling back the caller does not help when the
dependency is the cause. Changes that affect traffic routing between several services are
high risk and must be handled by an operator.
