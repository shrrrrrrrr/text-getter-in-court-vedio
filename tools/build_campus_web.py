"""Build a complete web delta bundle; ASR/CUDA weights stay on the prepared server."""
import argparse
import hashlib
import io
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import tarfile
from pathlib import Path

import httpx

try:
    from . import build_campus_offline as base
    from . import campus_prepare as prep
except ImportError:
    import build_campus_offline as base
    import campus_prepare as prep

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / '.state/web-assets'
REQUIREMENTS = ['pip==25.3', 'fastapi==0.162.0', 'starlette==1.7.0', 'uvicorn==0.54.0',
                'python-multipart==0.0.32', 'pydantic==2.14.1', 'httpx==0.28.1', 'pytest==9.2.0']
FFMPEG_NAME = 'ffmpeg-n9.0.2-37-gae19868753-linux64-gpl-9.0.tar.xz'
FFMPEG_URL = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-28-19-47/' + FFMPEG_NAME
FFMPEG_SIZE = 153115468
FFMPEG_SHA = 'd5824906a9f3cae531f9f8b830b20fe51e7a422d8ed6fcdef3be4d5209b1b0a1'


def prepare_assets():
    wheels = ASSETS / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    for _ in range(15):
        selected, missing = base.dependency_closure(wheels, '3.10', REQUIREMENTS)
        if not missing:
            break
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--index-url', 'https://pypi.org/simple', '--no-deps',
                        '--only-binary=:all:', '--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_27_x86_64',
                        '--platform', 'manylinux_2_28_x86_64', '--python-version', '3.10', '--implementation', 'cp', '--abi', 'cp310',
                        '--dest', str(wheels)] + missing, check=True)
    else:
        raise RuntimeError('Web Linux dependency set did not resolve')
    with httpx.Client(timeout=httpx.Timeout(90, connect=20), follow_redirects=True) as client:
        provenance = [base.wheel_provenance(client, p) for p in selected]
        ffmpeg = ASSETS / FFMPEG_NAME
        base.fetch(client, ffmpeg, FFMPEG_SIZE, FFMPEG_SHA, [FFMPEG_URL])
    return selected, ffmpeg, provenance


def build():
    wheels, ffmpeg, provenance = prepare_assets()
    paths = [(p, 'wheels/' + p.name) for p in wheels] + [(ffmpeg, 'assets/ffmpeg.tar.xz')]
    for directory in ['court_transcript', 'static']:
        for p in sorted((ROOT / directory).rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts and (directory == 'static' or p.suffix == '.py'):
                paths.append((p, 'app/' + p.relative_to(ROOT).as_posix()))
    for name in ['campus_web_install.py', 'campus_prepare.py']:
        paths.append((ROOT / 'tools' / name, 'tools/' + name))
    credentials = ROOT / '.state/deploy/campus-access.local.json'
    credentials.parent.mkdir(parents=True, exist_ok=True)
    if not credentials.exists():
        records = [{'id': secrets.token_hex(16), 'label': '成员 ' + str(n), 'credential': 'court_' + secrets.token_urlsafe(32)} for n in range(1, 6)]
        fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2)
    paths.append((credentials, 'private/access-seeds.json'))
    manifest = {'schema': 2, 'python_abi': 'cp310', 'version': '0.3.0',
                'files': [base.file_row(p, name) for p, name in paths], 'wheel_sources': provenance,
                'ffmpeg_source': {'url': FFMPEG_URL, 'sha256': FFMPEG_SHA}, 'model_weights_included': False}
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    fingerprint = hashlib.sha256(raw).hexdigest()
    dest = ROOT / '.state/web-bundles'
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / ('court-web-cp310-' + fingerprint[:12] + '.tar')
    if not archive.exists():
        with tarfile.open(archive.with_suffix('.partial.tar'), 'w') as tar:
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(raw), 0o600
            tar.addfile(info, io.BytesIO(raw))
            for p, name in paths:
                tar.add(p, arcname=name, recursive=False)
        archive.with_suffix('.partial.tar').replace(archive)
    receipt = {'archive': str(archive), 'sha256': prep.digest(archive), 'size': archive.stat().st_size,
               'manifest_sha256': fingerprint, 'python_abi': 'cp310', 'files': len(paths) + 1}
    (dest / 'current-web-transfer.json').write_text(json.dumps(receipt, indent=2), 'utf-8')
    print('WEB_OFFLINE_BUNDLE_READY ' + json.dumps(receipt), flush=True)
    return receipt


def server_commands(upload_id, sha, listen, port=8766):
    if not __import__('re').fullmatch('[a-f0-9]{12}', upload_id) or not __import__('re').fullmatch('[a-f0-9]{64}', sha):
        raise ValueError('Invalid upload identity')
    ip = ipaddress.IPv4Address(listen)
    if not ip.is_private or ip.is_loopback or ip.is_unspecified or not 1024 <= port <= 65535:
        raise ValueError('Use an explicit campus IPv4 and unprivileged port')
    return '\n'.join(['(', 'set -eu', 'umask 077', "court_sha='" + sha + "'",
        'court_archive="$HOME/court-asr-upload.' + upload_id + '/bundle-$court_sha.tar"',
        'printf \'%s  %s\\n\' "$court_sha" "$court_archive" | sha256sum --check -',
        'court_payload=$(mktemp -d "$(dirname "$court_archive")/web-payload.XXXXXX")',
        'tar --extract --file="$court_archive" --directory="$court_payload" --no-same-owner --no-same-permissions',
        'sudo python3 "$court_payload/tools/campus_web_install.py" --bundle "$court_payload" --listen ' + str(ip) +
        ' --port ' + str(port) + ' --gpu 3 --allow-campus-http', ')', ''])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets-only', action='store_true')
    parser.add_argument('--upload-id')
    parser.add_argument('--sha256')
    parser.add_argument('--listen')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if args.upload_id:
        print(server_commands(args.upload_id, args.sha256, args.listen, args.port), end='')
    else:
        prepare_assets() if args.assets_only else build()
