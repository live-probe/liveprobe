---
title: 'Kubernetes docs - Debug Pods: My service is missing endpoints'
source: kubernetes-docs
kind: public_doc
family:
- endpoint_loss
source_uri: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/#my-service-is-missing-endpoints
upstream: https://github.com/kubernetes/website/blob/fcf148c070b140cbd13ec65d5809f0686e680a9c/content/en/docs/tasks/debug/debug-application/debug-pods.md#L163-L184
upstream_line_map:
- 163
- 164
- 165
- 166
- 167
- 168
- 169
- 170
- 171
- 172
- 173
- 174
- 175
- 176
- 177
- 178
- 179
- 180
- 181
- 182
- 183
license: CC-BY-4.0
attribution: '"Debug Pods" by The Kubernetes Authors, kubernetes.io, CC BY 4.0'
retrieved_at: '2026-09-26T06:30:00Z'
changes: Excerpt of one section; no text changes.
---

#### My service is missing endpoints

If you are missing endpoints, try listing Pods using the labels in the Service selector.
For example, that you have a Service with the following selector:

```yaml
...
spec:
  selector:
    name: nginx
    type: frontend
```

You can use:

```shell
kubectl get pods --selector=name=nginx,type=frontend
```

to list pods that match this selector. Verify that the list matches the Pods that you expect to provide your Service.
Verify that the pod's `containerPort` matches the Service's `targetPort`
