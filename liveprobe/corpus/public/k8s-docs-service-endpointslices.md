---
title: 'Kubernetes docs - Debug Services: Does the Service have any EndpointSlices?'
source: kubernetes-docs
kind: public_doc
family:
- endpoint_loss
source_uri: https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/#does-the-service-have-any-endpointslices
upstream: https://github.com/kubernetes/website/blob/fcf148c070b140cbd13ec65d5809f0686e680a9c/content/en/docs/tasks/debug/debug-application/debug-service.md#L420-L463
upstream_line_map:
- 420
- 421
- 422
- 423
- 424
- 425
- 426
- 427
- 428
- 429
- 430
- 431
- 432
- 433
- 434
- 435
- 436
- 437
- 438
- 439
- 440
- 441
- 442
- 443
- 444
- 445
- 446
- 447
- 448
- 449
- 450
- 451
- 452
- 453
- 454
- 455
- 456
- 457
- 458
- 459
- 460
- 461
- 462
- 463
license: CC-BY-4.0
attribution: '"Debug Services" by The Kubernetes Authors, kubernetes.io, CC BY 4.0'
retrieved_at: '2026-09-26T06:30:00Z'
changes: Excerpt of one section; no text changes.
---

## Does the Service have any EndpointSlices?

If you got this far, you have confirmed that your Service is correctly
defined and is resolved by DNS.  Now let's check that the Pods you ran are
actually being selected by the Service.

Earlier you saw that the Pods were running.  You can re-check that:

```shell
kubectl get pods -l app=hostnames
```
```none
NAME                        READY     STATUS    RESTARTS   AGE
hostnames-632524106-bbpiw   1/1       Running   0          1h
hostnames-632524106-ly40y   1/1       Running   0          1h
hostnames-632524106-tlaok   1/1       Running   0          1h
```

The `-l app=hostnames` argument is a label selector configured on the Service.

The "AGE" column says that these Pods are about an hour old, which implies that
they are running fine and not crashing.

The "RESTARTS" column says that these pods are not crashing frequently or being
restarted.  Frequent restarts could lead to intermittent connectivity issues.
If the restart count is high, read more about how to [debug pods](/docs/tasks/debug/debug-application/debug-pods).

Inside the Kubernetes system is a control loop which evaluates the selector of
every Service and saves the results into one or more EndpointSlice objects.

```shell
kubectl get endpointslices -l kubernetes.io/service-name=hostnames

NAME              ADDRESSTYPE   PORTS   ENDPOINTS
hostnames-ytpni   IPv4          9376    10.244.0.5,10.244.0.6,10.244.0.7
```

This confirms that the EndpointSlice controller has found the correct Pods for
your Service.  If the `ENDPOINTS` column is `<none>`, you should check that
the `spec.selector` field of your Service actually selects for
`metadata.labels` values on your Pods.  A common mistake is to have a typo or
other error, such as the Service selecting for `app=hostnames`, but the
Deployment specifying `run=hostnames`, as in versions previous to 1.18, where
the `kubectl run` command could have been also used to create a Deployment.
