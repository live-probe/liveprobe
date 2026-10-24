---
title: OOMKilled containers and memory pressure
source: internal-runbook
applies_to: [OOMKilled]
risk: medium
---

# OOMKilled containers and memory pressure

## Symptoms
Container `lastState.terminated.reason` is `OOMKilled`, restart count increases,
and the pod may enter `CrashLoopBackOff`. Memory usage approaches the container limit before each restart.

## Typical root causes
- The memory limit is too low for the workload (for JVM services, heap + metaspace + threads exceed the limit).
- A memory leak introduced by a new version.
- A traffic spike causing large in-memory buffers.

## Diagnosis
1. Compare container memory usage over time with the configured limit.
2. Check if OOM kills started after a rollout (new version) or after a traffic change.
3. For JVMs check `-XX:MaxRAMPercentage` and heap settings.

## Remediation
- If caused by a recent rollout: roll back to the previous revision.
- If the limit is too small: raising the limit is a configuration change that needs operator approval.
- Never remove memory limits entirely.
