---
title: 'Kubernetes docs - Images: ImagePullBackOff'
source: kubernetes-docs
kind: public_doc
family:
- image_crash
source_uri: https://kubernetes.io/docs/concepts/containers/images/#imagepullbackoff
upstream: https://github.com/kubernetes/website/blob/fcf148c070b140cbd13ec65d5809f0686e680a9c/content/en/docs/concepts/containers/images.md#L176-L188
upstream_line_map:
- 176
- 177
- 178
- 179
- 180
- 181
- 182
- 183
- 184
- 185
- 186
- 187
- 188
license: CC-BY-4.0
attribution: '"Images" by The Kubernetes Authors, kubernetes.io, CC BY 4.0'
retrieved_at: '2026-09-26T06:30:00Z'
changes: Excerpt of one section; no text changes.
---

### ImagePullBackOff

When a kubelet starts creating containers for a Pod using a container runtime,
it might be possible the container is in [Waiting](/docs/concepts/workloads/pods/pod-lifecycle/#container-state-waiting)
state because of `ImagePullBackOff`.

The status `ImagePullBackOff` means that a container could not start because Kubernetes
could not pull a container image (for reasons such as invalid image name, or pulling
from a private registry without `imagePullSecret`). The `BackOff` part indicates
that Kubernetes will keep trying to pull the image, with an increasing back-off delay.

Kubernetes raises the delay between each attempt until it reaches a compiled-in limit,
which is 300 seconds (5 minutes).
