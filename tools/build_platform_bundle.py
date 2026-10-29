"""Build a small offline platform preparation bundle; no model, wheels or private keys."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def build(public_key, baseline_record, output):
    public = public_key.read_bytes()
    der = base64.b64decode(b''.join(public.splitlines()[1:-1]), validate=True)
    if (public.splitlines()[0] != b'-----BEGIN PUBLIC KEY-----' or len(der) != 44
            or der[:12] != bytes.fromhex('302a300506032b6570032100')):
        raise ValueError('Use the reviewed Ed25519 platform public key, never a private key')
    observed = json.loads(baseline_record.read_text('utf-8'))['campus']
    baseline = {name: observed[name] for name in ('app_sha256', 'access_sha256')}
    if not all(re.fullmatch('[a-f0-9]{64}', value) for value in baseline.values()):
        raise ValueError('Invalid current source baseline')
    files = {'platform-public.pem': public}
    for folder in ('court_transcript', 'static'):
        for path in sorted((ROOT / folder).rglob('*')):
            if (not path.is_file() or path.is_symlink() or '__pycache__' in path.parts
                    or 'fonts' in path.parts or path.suffix == '.pyc'):
                continue
            files['app/' + path.relative_to(ROOT).as_posix()] = path.read_bytes()
    files['tools/campus_platform_prepare.py'] = (ROOT / 'tools/campus_platform_prepare.py').read_bytes()
    font = ROOT / 'static/fonts/ChillReunion-Round.woff2'
    manifest = {'schema': 3, 'version': '0.3.0',
        'baseline_app_sha256': baseline['app_sha256'], 'baseline_access_sha256': baseline['access_sha256'],
        'reuse_assets': [{'path': 'static/fonts/ChillReunion-Round.woff2', 'sha256': sha(font.read_bytes())}],
        'files': [{'path': name, 'size': len(raw), 'sha256': sha(raw)} for name, raw in sorted(files.items())],
        'private_key_included': False, 'model_weights_included': False, 'dependency_changes': False}
    raw = json.dumps(manifest, indent=2).encode()
    fingerprint = sha(raw)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('court-platform-' + fingerprint[:20] + '.tar')
    if path.exists():
        raise ValueError('Preparation archive already exists; verify the saved receipt instead of replacing it')
    with tarfile.open(path, 'w') as archive:
        for name, data in [('manifest.json', raw), *sorted(files.items())]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    receipt = {'archive': str(path.resolve()), 'sha256': sha(path.read_bytes()), 'size': path.stat().st_size,
               'manifest_sha256': fingerprint, 'files': len(files), 'font_reused': True,
               'models_or_wheels_included': False, 'private_keys_included': False}
    return receipt


def server_commands(directory, digest, account):
    if (not re.fullmatch(r'/[h]ome/[A-Za-z0-9_.-]+/court-asr-upload\.[a-f0-9]{12}', directory)
            or not re.fullmatch('[a-f0-9]{64}', digest) or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', account)):
        raise ValueError('Invalid verified upload destination')
    if PurePosixPath(directory).parts[1:3] != ('home', account):
        raise ValueError('Account and upload home differ')
    return f'''(
set -eu
umask 077
court_archive='{directory}/bundle-{digest}.tar'
printf '%s  %s\\n' '{digest}' "$court_archive" | sha256sum --check -
court_payload=$(mktemp -d '{directory}/platform-payload.XXXXXX')
tar --extract --file="$court_archive" --directory="$court_payload" --no-same-owner --no-same-permissions
sudo /usr/bin/python3 -I -B "$court_payload/tools/campus_platform_prepare.py" --bundle "$court_payload" --account '{account}'
)
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-key', type=Path)
    parser.add_argument('--baseline-record', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / '.state/platform-bundles')
    parser.add_argument('--server-directory')
    parser.add_argument('--sha256')
    parser.add_argument('--account')
    args = parser.parse_args()
    if args.server_directory:
        print(server_commands(args.server_directory, args.sha256, args.account), end='')
    else:
        if not args.public_key or not args.baseline_record:
            parser.error('--public-key and --baseline-record are required to build')
        receipt = build(args.public_key, args.baseline_record, args.output)
        path = ROOT / '.state/deploy/platform-bundle.local.json'
        path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print('COURT_PLATFORM_PREPARATION_BUNDLE_BUILT ' + json.dumps({'size': receipt['size'], 'files': receipt['files']}))
