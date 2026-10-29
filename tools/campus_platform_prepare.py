"""Stage a verified platform integration release and a private TLS key; do not activate.

Stdlib, Python 3.10+. Existing users, services, runtime env and databases are untouched.
Only the operator's root-owned preparation directory is written.
"""
import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import socket
import stat
import subprocess
import sys


class Refusal(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def read(path, maximum=8 * 1024 * 1024):
    require(path.resolve() == path, 'linked input refused')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        item = os.fstat(source.fileno())
        require(stat.S_ISREG(item.st_mode) and item.st_nlink == 1 and item.st_size <= maximum, 'unsafe input file')
        raw = source.read(maximum + 1)
    require(len(raw) <= maximum, 'input grew during read')
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())


def command(args):
    result = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=20,
        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
    require(result.returncode == 0, 'fixed preparation command failed')
    return result.stdout


def verify_bundle(bundle):
    raw = read(bundle / 'manifest.json', 65536)
    value = json.loads(raw)
    require(value.get('schema') == 3 and value.get('version') in ('0.3.0', '0.3.1'), 'unexpected integration bundle')
    require(10 <= len(value.get('files', [])) <= 100, 'unexpected file count')
    seen = set()
    total = 0
    for row in value['files']:
        name = row['path']
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and path.as_posix() == name and name not in seen
            and re.fullmatch(r'(?:app/(?:court_transcript|static)/[A-Za-z0-9_./-]+|platform-public.pem|tools/campus_platform_prepare.py)', name),
            'unexpected bundle member')
        seen.add(name)
        data = read(bundle / name)
        require(len(data) == row['size'] and digest(data) == row['sha256'], 'bundle checksum differs')
        if name.endswith('.py'):
            ast.parse(data, filename=name, feature_version=(3, 10))
        total += len(data)
    require(total < 32 * 1024 * 1024 and {'platform-public.pem', 'app/court_transcript/platform_auth.py',
        'app/court_transcript/uploads.py', 'app/court_transcript/bridge.py', 'app/static/index.html'} <= seen,
        'incomplete integration bundle')
    return value, digest(raw)


def baseline(home, uid, unit, manifest, run=command):
    root = home / 'court-transcript-web-v1'
    require(json.loads(read(root / 'owner.json')) == {'profile': 'court-transcript-campus-web-v1', 'uid': uid},
            'existing application owner differs')
    unit_text = read(unit).decode()
    require('Description=Court transcript campus web v1\n' in unit_text, 'unexpected web unit')
    current = re.findall(r'^WorkingDirectory=(.+)$', unit_text, re.M)
    require(len(current) == 1, 'ambiguous current release')
    release = Path(current[0])
    require(release.parent == root / 'releases' and release.resolve() == release, 'unexpected release directory')
    for name in ('app', 'access'):
        require(digest(read(release / 'court_transcript' / (name + '.py'))) == manifest['baseline_' + name + '_sha256'],
                'live source changed; new inspection required')
    for asset in manifest.get('reuse_assets', []):
        require(asset['path'] == 'static/fonts/ChillReunion-Round.woff2' and
                digest(read(release / asset['path'])) == asset['sha256'], 'reused asset differs')
    active = run(['systemctl', 'is-active', 'court-transcript-web.service']).decode().strip()
    require(active == 'active', 'existing court service is not active')
    return {'root': str(root), 'release': str(release), 'unit_sha256': digest(unit_text.encode()),
            'runtime_env_sha256': digest(read(root / 'runtime.env')), 'unit_path': str(unit)}


def prepare(bundle, manifest, fingerprint, observed, parent=Path('/root/court-platform-preparations'), run=command):
    require(parent.resolve() == parent, 'linked preparation parent refused')
    if not parent.exists():
        parent.mkdir(mode=0o700)
    item = parent.stat()
    require(stat.S_ISDIR(item.st_mode) and item.st_uid == os.geteuid() and not item.st_mode & 0o077,
            'unsafe preparation parent')
    target = parent / fingerprint[:20]
    if target.exists() or target.is_symlink():
        previous = json.loads(read(target / 'result.json', 65536))
        require(previous.get('manifest_sha256') == fingerprint and previous.get('phase') == 'prepared',
                'partial or unknown preparation; retained for review')
        require(previous['baseline'] == observed, 'live baseline changed since preparation')
        for row in manifest['files']:
            require(digest(read(target / row['path'])) == row['sha256'], 'prepared source differs')
        require(digest(read(target / 'server.crt')) == previous['certificate_pem_sha256'], 'prepared certificate differs')
        require(digest(read(target / 'server.key')) == previous['private_key_file_sha256'], 'prepared key differs')
        require(digest(read(target / 'id_ed25519')) == previous['ssh_private_file_sha256']
                and read(target / 'id_ed25519.pub').decode().strip() == previous['ssh_public_key'], 'prepared SSH identity differs')
        # Never include the key's file hash in the public result either.
        return public_result(previous)
    target.mkdir(mode=0o700)
    write_new(target / 'manifest.json', read(bundle / 'manifest.json'))
    for row in manifest['files']:
        destination = target / row['path']
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = read(bundle / row['path'])
        require(digest(data) == row['sha256'], 'bundle changed while staging')
        write_new(destination, data)
    # Generate key only on the campus server; it never enters the upload or response.
    run(['/usr/bin/openssl', 'req', '-new', '-x509', '-newkey', 'ed25519', '-nodes',
         '-keyout', str(target / 'server.key'), '-out', str(target / 'server.crt'), '-days', '365',
         '-subj', '/CN=court-transcript.internal', '-addext', 'subjectAltName=DNS:court-transcript.internal'])
    (target / 'server.key').chmod(0o600)
    (target / 'server.crt').chmod(0o600)
    run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'court-platform-link',
         '-f', str(target / 'id_ed25519')])
    (target / 'id_ed25519').chmod(0o600)
    public_line = read(target / 'id_ed25519.pub', 2048).decode().strip()
    parts = public_line.split()
    require(len(parts) == 3 and parts[0] == 'ssh-ed25519' and parts[2] == 'court-platform-link', 'unexpected SSH public identity')
    blob = base64.b64decode(parts[1], validate=True)
    require(len(blob) == 51 and blob[:19] == bytes.fromhex('0000000b7373682d6564323535313900000020'), 'invalid SSH public identity')
    certificate = read(target / 'server.crt')
    result = {'schema': 1, 'phase': 'prepared', 'manifest_sha256': fingerprint,
        'preparation_directory': str(target), 'baseline': observed,
        'certificate_pem_base64': base64.b64encode(certificate).decode(), 'certificate_pem_sha256': digest(certificate),
        'private_key_file_sha256': digest(read(target / 'server.key')),
        'ssh_public_key': public_line,
        'ssh_public_fingerprint': 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('='),
        'ssh_private_file_sha256': digest(read(target / 'id_ed25519')),
        'tls_server_name': 'court-transcript.internal', 'planned_campus_tls_listener': '127.0.0.1:18766',
        'planned_cloud_listener': '127.0.0.1:28100', 'private_key_generated_on_campus_only': True,
        'services_changed': False, 'linux_accounts_or_groups_changed': False,
        'model_or_generation_requested': False, 'activation_performed': False}
    write_new(target / 'result.json', json.dumps(result, indent=2).encode())
    return public_result(result)


def public_result(value):
    fields = {'schema', 'phase', 'manifest_sha256', 'preparation_directory', 'baseline',
              'certificate_pem_base64', 'certificate_pem_sha256', 'tls_server_name',
              'planned_campus_tls_listener', 'planned_cloud_listener', 'private_key_generated_on_campus_only',
              'ssh_public_key', 'ssh_public_fingerprint',
              'services_changed', 'linux_accounts_or_groups_changed', 'model_or_generation_requested', 'activation_performed'}
    return {key: data for key, data in value.items() if key in fields}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'use existing sudo privileges')
    account = pwd.getpwnam(args.account)
    require(account.pw_uid >= 1000 and Path(account.pw_dir).parent == Path('/home'), 'use the existing campus account')
    home = Path(account.pw_dir)
    require(home.resolve() == home and re.fullmatch(r'/[A-Za-z0-9_./-]+', str(home)), 'unexpected home path')
    manifest, fingerprint = verify_bundle(args.bundle.resolve())
    observed = baseline(home, account.pw_uid, Path('/etc/systemd/system/court-transcript-web.service'), manifest)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 18766))
    print(json.dumps(prepare(args.bundle.resolve(), manifest, fingerprint, observed), indent=2), flush=True)
    print('COURT_PLATFORM_CAMPUS_PREPARED_NOT_ACTIVATED', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No raw subprocess output, configuration, keys or tracebacks.
        print('COURT_PLATFORM_PREPARE_STOPPED: ' + (str(exc) if isinstance(exc, Refusal) else type(exc).__name__), flush=True)
        sys.exit(1)
