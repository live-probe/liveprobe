#!/usr/bin/env bash
# Live NetworkPolicy connectivity matrix (KAN-63). Opens real TCP connections from inside LiveProbe pods
# (kubectl exec with the operator kubeconfig) and compares the outcome with the expected policy.
#   scripts/netpol-check.sh            # assert the enforced matrix (exit 1 on any mismatch)
#   scripts/netpol-check.sh --observe  # print outcomes only (e.g. baseline before policies are applied)
# Outcomes: open | blocked (connect timed out = dropped) | refused (TCP RST / ICMP reject) | dns-fail.
# The expectation "denied" accepts blocked or refused: kube-router (K3s) rejects some denied egress
# instead of dropping it. Every denied destination has a positive control in the same run (an allowed
# peer that reaches the same Service:port), so a refusal cannot be a closed port.
# Nothing is written to any workload; only short TCP connects and name lookups are made.
set -uo pipefail
CTX="${KUBE_CONTEXT:-rancher-desktop}"
OBSERVE=0; [[ "${1:-}" == "--observe" ]] && OBSERVE=1
EXT_HOST="${EXT_HOST:-api.openai.com}"   # LLM provider endpoint used by liveprobe-api
T=3
PASS=0 FAIL=0

SYS=liveprobe-system OBS=liveprobe-observability TGT=liveprobe-target
PG=postgres.$SYS.svc; API=liveprobe-api.$SYS.svc; DASH=dashboard.$SYS.svc
PROM=prometheus.$OBS.svc; LOKI=loki.$OBS.svc; GRAF=grafana.$OBS.svc; AM=alertmanager.$OBS.svc; KSM=kube-state-metrics.$OBS.svc
ORDER=order.$TGT.svc; INV=inventory.$TGT.svc; KAPI=kubernetes.default.svc

probe() { # ns workload tool host port -> prints outcome
  local ns="$1" wl="$2" tool="$3" host="$4" port="$5" out rc
  case "$tool" in
    nc)
      out="$(kubectl --context "$CTX" -n "$ns" exec "$wl" -- nc -z -w "$T" "$host" "$port" 2>&1)"; rc=$?
      if [[ $rc -eq 0 ]]; then echo open
      elif [[ "$out" == *"bad address"* ]]; then echo dns-fail
      elif [[ "$out" == *"refused"* ]]; then echo refused
      else echo blocked; fi ;;
    py)
      kubectl --context "$CTX" -n "$ns" exec "$wl" -- python3 -c '
import socket, sys
h, p, t = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    socket.getaddrinfo(h, p)
except socket.gaierror:
    print("dns-fail"); sys.exit()
try:
    socket.create_connection((h, p), timeout=t).close(); print("open")
except ConnectionRefusedError:
    print("refused")
except OSError:
    print("blocked")' "$host" "$port" "$T" 2>/dev/null || echo error ;;
    bash)
      out="$(kubectl --context "$CTX" -n "$ns" exec "$wl" -- bash -c \
        "getent hosts $host >/dev/null || { echo dns-fail; exit; }; timeout $T bash -c '</dev/tcp/$host/$port' 2>&1 && echo open || echo closed" 2>&1)"
      if [[ "$out" == *dns-fail* ]]; then echo dns-fail
      elif [[ "$out" == *refused* ]]; then echo refused
      elif [[ "$out" == *$'\nopen'* || "$out" == open ]]; then echo open
      else echo blocked; fi ;;
  esac
}

expect() { # want src_ns src_wl tool host port label
  local want="$1" got
  got="$(probe "$2" "$3" "$4" "$5" "$6")"
  if [[ $OBSERVE -eq 1 ]]; then
    printf '%-8s %-40s -> %-45s (%s)\n' "$got" "$2/${3#deploy/}" "$5:$6" "$7"; return
  fi
  local ok=0
  [[ "$got" == "$want" ]] && ok=1
  [[ "$want" == denied && ( "$got" == blocked || "$got" == refused ) ]] && ok=1
  if [[ $ok -eq 1 ]]; then
    printf 'PASS %-8s %-40s -> %-45s %s\n' "$got" "$2/${3#deploy/}" "$5:$6" "$7"; PASS=$((PASS + 1))
  else
    printf 'FAIL want=%s got=%s %s -> %s:%s %s\n' "$want" "$got" "$2/${3#deploy/}" "$5" "$6" "$7" >&2; FAIL=$((FAIL + 1))
  fi
}

echo "== liveprobe-api (allowed: DB, Prometheus, Loki, K8s API, LLM HTTPS, DNS)"
A=(liveprobe-system deploy/liveprobe-api py)
expect open    "${A[@]}" $PG    5432 "app database"
expect open    "${A[@]}" $PROM  9090 "evidence metrics"
expect open    "${A[@]}" $LOKI  3100 "evidence logs"
expect open    "${A[@]}" $KAPI  443  "kubernetes API"
expect open    "${A[@]}" "$EXT_HOST" 443 "LLM provider HTTPS"
expect denied  "${A[@]}" $GRAF  3000 "grafana"
expect denied  "${A[@]}" $AM    9093 "alertmanager"
expect denied  "${A[@]}" $KSM   8080 "kube-state-metrics"
expect denied  "${A[@]}" $ORDER 8080 "target app (API uses K8s API, not app traffic)"
expect denied  "${A[@]}" "$EXT_HOST" 80 "plain HTTP egress"

echo "== dashboard (static SPA: no egress except DNS)"
D=(liveprobe-system deploy/dashboard nc)
expect denied  "${D[@]}" $PG   5432 "database"
expect denied  "${D[@]}" $API  8000 "api (browser reaches API through ingress)"
expect denied  "${D[@]}" $KAPI 443  "kubernetes API"
expect denied  "${D[@]}" "$EXT_HOST" 443 "internet"

echo "== target namespace (loadgen / order): own namespace only"
L=(liveprobe-target deploy/loadgen nc)
expect open    "${L[@]}" $ORDER 8080 "order (same namespace)"
expect open    "${L[@]}" $INV   8080 "inventory (same namespace)"
expect denied  "${L[@]}" $PG    5432 "LiveProbe database"
expect denied  "${L[@]}" $API   8000 "LiveProbe API"
expect denied  "${L[@]}" $DASH  80   "dashboard"
expect denied  "${L[@]}" $PROM  9090 "prometheus"
expect denied  "${L[@]}" $LOKI  3100 "loki"
expect denied  "${L[@]}" $KAPI  443  "kubernetes API"
expect denied  "${L[@]}" "$EXT_HOST" 443 "internet"
O=(liveprobe-target deploy/order bash)
expect open    "${O[@]}" $INV   8080 "order -> inventory"
expect denied  "${O[@]}" $PG    5432 "LiveProbe database"
I=(liveprobe-target deploy/inventory bash)
expect open    "${I[@]}" $PG    5432 "inventory -> postgres (dedicated inventory DB, KAN-47)"
expect denied  "${I[@]}" $API   8000 "LiveProbe API"
expect denied  "${I[@]}" $PROM  9090 "prometheus"
expect denied  "${I[@]}" $KAPI  443  "kubernetes API"
expect denied  "${I[@]}" "$EXT_HOST" 443 "internet"

echo "== observability"
P=(liveprobe-observability deploy/prometheus nc)
expect open    "${P[@]}" $KSM   8080 "scrape kube-state-metrics"
expect open    "${P[@]}" $AM    9093 "alert delivery"
expect open    "${P[@]}" $ORDER 8080 "scrape target pods"
expect open    "${P[@]}" $KAPI  443  "service discovery"
expect denied  "${P[@]}" $PG    5432 "database"
expect denied  "${P[@]}" $API   8000 "api"
expect denied  "${P[@]}" "$EXT_HOST" 443 "internet"
M=(liveprobe-observability deploy/alertmanager nc)
expect open    "${M[@]}" $API   8000 "webhook to LiveProbe API"
expect denied  "${M[@]}" $PG    5432 "database"
expect denied  "${M[@]}" $ORDER 8080 "target"
expect denied  "${M[@]}" "$EXT_HOST" 443 "internet"
G=(liveprobe-observability deploy/grafana nc)
expect open    "${G[@]}" $PROM  9090 "datasource prometheus"
expect open    "${G[@]}" $LOKI  3100 "datasource loki"
expect denied  "${G[@]}" $PG    5432 "database"
expect denied  "${G[@]}" $API   8000 "api"
expect denied  "${G[@]}" $KAPI  443  "kubernetes API"
expect denied  "${G[@]}" "$EXT_HOST" 443 "internet"

echo "== ingress controller path (host -> ingress -> pods)"
for url in "http://${LP_HOST:-liveprobe.localhost}/" "http://${LP_HOST:-liveprobe.localhost}/api/healthz" "http://${GRAFANA_HOST:-grafana.localhost}/api/health"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url")"
  if [[ $OBSERVE -eq 1 ]]; then printf '%-8s %s\n' "$code" "$url"
  elif [[ "$code" == 200 ]]; then printf 'PASS 200      ingress -> %s\n' "$url"; PASS=$((PASS + 1))
  else printf 'FAIL ingress %s -> %s\n' "$url" "$code" >&2; FAIL=$((FAIL + 1)); fi
done

[[ $OBSERVE -eq 1 ]] && exit 0
printf 'NETPOL: %s passed, %s failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
