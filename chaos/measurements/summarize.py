"""Summarize measured fixture outcomes without turning them into v1 passes."""
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
source = ROOT / 'docs/evidence/KAN-110/measurements.json'
d = json.loads(source.read_text())

def spread(rows):
    rows = sorted(x for x in rows if x is not None)
    return {'n': len(rows), 'min': rows[0] if rows else None,
            'median': statistics.median(rows) if rows else None,
            'max': rows[-1] if rows else None, 'samples': rows}

summary = {
    'crashloop_wait_s': spread([x['first_crashloop_s'] for x in d['crash']]),
    'crashloop_and_two_restarts_s': spread([x['joint_s'] for x in d['crash']]),
    'oomkilled_s': spread([x['oom_s'] for x in d['oom']]),
    'oom_reason_exit': [{'reason': x['reason'], 'exit': x['exit']} for x in d['oom']],
    'latency_100ms_median_delta_ms': spread([x['median_delta_ms'] for x in d['latency']]),
    'endpoint_zero_and_caller_502_s': spread([x['first_joint_s'] for x in d['endpoint']]),
    'endpoint_recovery': [x['recovery'] for x in d['endpoint']],
    'pool_active_peak': {size: [max((s['physical_active'] for s in x['counts']), default=0)
                                for x in trials] for size, trials in d['pool'].items()},
    'pool_release_active': {size: [x['released']['physical_active'] for x in trials]
                            for size, trials in d['pool'].items()},
    'pool_pending_at_capacity': {size: [x['full']['pending'] for x in trials]
                                 for size, trials in d['pool'].items()},
    'pool_checkout_timeout_s': {size: spread([x['contender']['second'] for x in trials])
                                for size, trials in d['pool'].items()},
    'pool_retry_success': {size: [x['retry']['outcome'] == 'success' for x in trials]
                           for size, trials in d['pool'].items()},
    'pool_holder_release_s': {size: spread([max(h['second'] for h in x['holders'])
                                            for x in trials])
                              for size, trials in d['pool'].items()},
    'counts': {kind: len(d[kind]) for kind in ('crash', 'oom', 'latency', 'endpoint')},
    'final_ready': d.get('final_ready'), 'final_caller': d.get('final_caller'),
}
(ROOT / 'docs/evidence/KAN-110/summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
