"""Repeat ten-pair latency samples while the disposable kind fixture is alive."""
import json
from pathlib import Path
import subprocess
import time
import run

p = subprocess.Popen(['kubectl', '--kubeconfig', str(run.KUBECONFIG), '--context', run.CONTEXT,
                      '-n', run.NS, 'port-forward', 'pod/target', '18080:8080'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            if run.http('/health')['status'] == 200:
                break
        except Exception:
            time.sleep(.25)
    else:
        raise RuntimeError('port-forward not ready')
    file = run.OUT / 'measurements.json'
    d = json.loads(file.read_text())
    d['latency_pairs'] = [run.latency_trial(i) for i in range(1, 6)]
    run.write('measurements.json', d)
finally:
    p.terminate()
    p.wait(timeout=5)
