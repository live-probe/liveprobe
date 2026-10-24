#!/usr/bin/env python3
"""로컬 nginx 런타임과 검증된 dist로 네트워크 없는 화면 이미지를 만든다.
기본 Dockerfile 빌드의 대체 경로이며 새 기반 이미지·npm 설치 검증을 대신하지 않는다.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tarfile
import tempfile

BASE = 'liveprobe/dashboard@sha256:d0b2eac8ab6a3ca3b86efe4057f141e5012d8275fea9e80af86816aad2024efc'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    image = f'liveprobe/dashboard:{args.tag}'
    if subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise SystemExit('기존 태그는 덮어쓰지 않습니다.')
    base = json.loads(subprocess.check_output(['docker', 'image', 'inspect', BASE], text=True))[0]
    assert BASE in base['RepoDigests'], '로컬 런타임 digest가 일치하지 않습니다.'
    assert base['Config']['Entrypoint'] == ['/docker-entrypoint.sh']
    assert base['Config']['Cmd'] == ['nginx', '-g', 'daemon off;']
    dist = ROOT / 'dashboard/dist'
    assert (dist / 'index.html').is_file(), 'npm run build가 먼저 필요합니다.'
    scratch = Path.home() / '.hermes/cache/scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dashboard-assets-', dir=scratch) as tmp:
        work = Path(tmp)
        cid = subprocess.check_output(['docker', 'create', base['Id']], text=True).strip()
        try:
            subprocess.run(['docker', 'export', '--output', str(work / 'base.tar'), cid], check=True, timeout=90)
        finally:
            subprocess.run(['docker', 'rm', cid], check=True, stdout=subprocess.DEVNULL)
        # 이전 화면 파일만 제외한다. 원본 로컬 이미지와 실행 중 컨테이너는 바꾸지 않는다.
        with tarfile.open(work / 'base.tar') as source, tarfile.open(work / 'runtime-rootfs.tar', 'w') as target:
            for member in source:
                name = member.name.lstrip('./').rstrip('/')
                if name == 'usr/share/nginx/html' or name.startswith('usr/share/nginx/html/'):
                    continue
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
        context = work / 'context'
        context.mkdir()
        shutil.copyfile(ROOT / 'dashboard/Dockerfile.assets', context / 'Dockerfile')
        shutil.copyfile(ROOT / 'dashboard/nginx.conf', context / 'nginx.conf')
        (work / 'runtime-rootfs.tar').rename(context / 'runtime-rootfs.tar')
        shutil.copytree(dist, context / 'dist')
        subprocess.run(['docker', 'build', '--pull=false', '--network=none', '-t', image, str(context)], check=True, timeout=180)
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
    ref = next(d for d in info['RepoDigests'] if d.startswith('liveprobe/dashboard@'))
    result = {'tag': args.tag, 'image': ref, 'base_image': BASE, 'method': '로컬 런타임 export + 정적 번들, 외부 다운로드 없음',
              'assets': {str(p.relative_to(dist)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dist.rglob('*')) if p.is_file()}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
