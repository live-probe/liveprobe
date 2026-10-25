#!/usr/bin/env python3
"""P16 정적 화면의 실제 제공 파일과 배포를 읽기 전용으로 검증한다."""
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'docs/evidence/P16'
release = json.loads((EVIDENCE / 'release.json').read_text())
deploy = json.loads(subprocess.check_output(['kubectl', '-n', 'liveprobe-system', 'get', 'deployment', 'dashboard', '-o', 'json']))
containers = deploy['spec']['template']['spec']['containers']
image = next(c['image'] for c in containers if c['name'] == 'dashboard')
assert image == release['image'], '실제 Deployment와 빌드 이미지가 다릅니다.'
assert deploy['status'].get('observedGeneration', 0) >= deploy['metadata']['generation']
assert deploy['status'].get('updatedReplicas', 0) == deploy['spec']['replicas']
assert deploy['status'].get('availableReplicas', 0) == deploy['spec']['replicas']
files = []
for rel, expected in release['assets'].items():
    local = (ROOT / 'dashboard/dist' / rel).read_bytes()
    with urlopen('http://liveprobe.localhost/' + rel, timeout=15) as response:
        served = response.read()
        status = response.status
    actual = hashlib.sha256(served).hexdigest()
    assert actual == expected == hashlib.sha256(local).hexdigest(), rel
    files.append({'file': rel, 'status': status, 'sha256': actual, 'matches': True})
with urlopen('http://liveprobe.localhost/api/overview', timeout=15) as response:
    assert response.status == 200
api = json.loads(subprocess.check_output(['kubectl', '-n', 'liveprobe-system', 'get', 'deployment', 'liveprobe-api', '-o', 'json']))
api_image = next(c['image'] for c in api['spec']['template']['spec']['containers'] if c['name'] == 'api')
assert api_image == 'liveprobe/liveprobe@sha256:434e6311c4f41d57fe8ec18b4c55f22b8d3e1b47ba4f43e25b4abfa741e66ff7', '백엔드 배포가 바뀌었습니다.'
result = {'deployment': 'liveprobe-system/dashboard', 'image': image, 'ready': True, 'api_unchanged': True, 'files': files}
(EVIDENCE / 'served-readback.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
