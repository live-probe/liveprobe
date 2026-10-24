#!/usr/bin/env bash
# Run on the local rancher-desktop cluster only, after scripts/install.sh.
# Two live fault injections; checks DB evidence and restores the target baseline.
set -euo pipefail
cd "$(dirname "$0")/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS=liveprobe-target
K=(kubectl --context "$CTX")
TIMEOUT="${E2E_TIMEOUT:-600}"
# KAN-96/KAN-118: expected inventory image = what the overlay renders (digest pin), not a hard-coded tag.
EXPECTED_IMAGE=$(chaos/expected-image.sh inventory)
active=0
cleanup() { if [[ "$active" -eq 1 ]]; then chaos/recover.sh || true; fi; }
trap cleanup EXIT

sql() {
  "${K[@]}" -n liveprobe-system exec postgres-0 -- psql -U liveprobe -d liveprobe -Atc "$1" 2>/dev/null | tr -d '\r'
}
baseline() {
  local image selector eps code
  image=$("${K[@]}" -n "$NS" get deploy inventory -o jsonpath='{.spec.template.spec.containers[0].image}')
  selector=$("${K[@]}" -n "$NS" get svc inventory -o jsonpath='{.spec.selector.app}')
  eps=$("${K[@]}" -n "$NS" get endpoints inventory -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)
  code=$("${K[@]}" -n "$NS" exec deploy/loadgen -- curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://order.$NS.svc:8080/api/orders/check?item=a")
  [[ "$image" == "$EXPECTED_IMAGE" && "$selector" == inventory && -n "$eps" && "$code" == 200 ]] || {
    echo "baseline FAIL: image=$image (want $EXPECTED_IMAGE) selector=$selector endpoints=[$eps] order=$code" >&2; return 1; }
  echo "baseline: image=$image selector=inventory endpoints>0 order=200"
}
poll() { # family desired_status min_id
  local family="$1" desired="$2" min_id="$3" deadline=$((SECONDS + TIMEOUT)) row id status level action_status executed runs citations evidence
  while (( SECONDS < deadline )); do
    row=$(sql "SELECT i.id||'|'||i.status||'|'||coalesce(i.severity_level,'')||'|'||coalesce((SELECT string_agg(a.status,',') FROM actions a WHERE a.incident_id=i.id),'')||'|'||(SELECT count(*) FROM actions a WHERE a.incident_id=i.id AND a.executed)||'|'||(SELECT count(*) FROM agent_runs r WHERE r.incident_id=i.id AND r.status='succeeded' AND r.provider='codex_cli' AND r.model<>'' AND r.duration_ms>0)||'|'||(SELECT count(*) FROM citations c JOIN agent_runs r ON r.id=c.agent_run_id WHERE r.incident_id=i.id)||'|'||(SELECT count(DISTINCT e.source) FROM evidence e WHERE e.incident_id=i.id) FROM incidents i WHERE i.id > $min_id AND i.family_hint = '$family' ORDER BY i.id DESC LIMIT 1" || true)
    if [[ -n "$row" ]]; then
      IFS='|' read -r id status level action_status executed runs citations evidence <<< "$row"
      if [[ "$status" == "$desired" ]]; then
        if [[ "$family" == image_pull ]]; then
          [[ "$level" == SIMPLE && "$action_status" == succeeded && "$executed" == 1 && "$runs" -ge 3 && "$citations" -ge 2 && "$evidence" -ge 3 ]] || { echo "Family1 incomplete: $row" >&2; return 1; }
          [[ "$("${K[@]}" -n "$NS" get deploy inventory -o jsonpath='{.spec.template.spec.containers[0].image}')" == "$EXPECTED_IMAGE" ]] || { echo 'Family1 image not undone' >&2; return 1; }
        else
          [[ "$level" == HIGH_RISK && "$executed" == 0 && "$runs" -ge 3 && "$citations" -ge 2 && "$evidence" -ge 3 ]] || { echo "Family4 incomplete: $row" >&2; return 1; }
        fi
        echo "PASS $family incident=$id status=$status level=$level executed=$executed real_agent_runs=$runs citations=$citations evidence_sources=$evidence"
        return 0
      fi
      if [[ "$status" == failed || ( "$status" == handed_off && "$desired" == resolved ) || ( "$status" == resolved && "$desired" == handed_off ) ]]; then
        echo "FAIL $family unexpected status: $row" >&2; return 1
      fi
    fi
    sleep 5
  done
  echo "FAIL $family timed out after ${TIMEOUT}s (last: ${row:-none})" >&2
  return 1
}

baseline
before=$(sql "SELECT coalesce(max(id),0) FROM incidents")
active=1
chaos/inject.sh family1
poll image_pull resolved "$before"
chaos/recover.sh
active=0
baseline
# Allow watcher residue from the first fault to settle before the next family.
before=$(sql "SELECT coalesce(max(id),0) FROM incidents")
active=1
chaos/inject.sh family4
poll endpoint_loss handed_off "$before"
chaos/recover.sh
active=0
baseline
python3 scripts/smoke-api.py
echo 'MVP-0 E2E PASS'
