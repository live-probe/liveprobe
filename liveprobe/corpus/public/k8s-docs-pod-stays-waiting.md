---
title: 'Kubernetes docs - Debug Pods: My pod stays waiting'
source: kubernetes-docs
kind: public_doc
family:
- image_crash
source_uri: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/#my-pod-stays-waiting
upstream: https://github.com/kubernetes/website/blob/fcf148c070b140cbd13ec65d5809f0686e680a9c/content/en/docs/tasks/debug/debug-application/debug-pods.md#L60-L70
upstream_line_map:
- 60
- 61
- 62
- 63
- 64
- 65
- 66
- 67
- 68
- 69
- 70
license: CC-BY-4.0
attribution: '"Debug Pods" by The Kubernetes Authors, kubernetes.io, CC BY 4.0'
retrieved_at: '2026-09-26T06:30:00Z'
changes: Excerpt of one section; no text changes.
---

#### My pod stays waiting

If a Pod is stuck in the `Waiting` state, then it has been scheduled to a worker node, 
but it can't run on that machine. Again, the information from `kubectl describe ...`
should be informative. The most common cause of `Waiting` pods is a failure to pull the image.
There are three things to check:

* Make sure that you have the name of the image correct.
* Have you pushed the image to the registry?
* Try to manually pull the image to see if the image can be pulled. For example,
  if you use Docker on your PC, run `docker pull <image>`.
