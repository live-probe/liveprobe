"""Feed measured-but-incomplete fixture evidence through the KAN-105 auditor."""
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/evidence/KAN-110'
data = json.loads((OUT / 'measurements.json').read_text())
rows = data['latency_pairs'][0]['pairs']
base = statistics.median(p['baseline_ms'] for p in rows)
active = statistics.median(p['active_ms'] for p in rows)
record = {'F3-01': {'case_id': 'F3-01', 'target': 'inventory', 'window_seconds': 10,
                    'observations': {'latency_samples': {
                        'source': 'http_probe', 'sample_count': len(rows),
                        'first_second': rows[0]['active_second'],
                        'last_second': rows[-1]['active_second'],
                        'observed': True, 'value': active, 'baseline_value': base,
                        'pairs': rows}},
                    'recovery': {field: False for field in (
                        'deployments_ready', 'services_endpoints_present', 'order_check_200',
                        'snapshot_restored', 'default_profile_404', 'pool_released')}}}
(OUT / 'kan105-input.json').write_text(json.dumps(record, indent=2) + '\n')
