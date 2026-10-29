"""Install the verified web delta, prove a loopback GPU workflow, then start its own service."""
import argparse
import hashlib
import http.cookiejar
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path, PurePosixPath

try:
    from . import campus_prepare as prep
except ImportError:
    import campus_prepare as prep

PROFILE = 'court-transcript-campus-web-v1'
UNIT = 'court-transcript-web.service'
DESCRIPTION = 'Court transcript campus web v1'


def verify_bundle(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text('utf-8'))
    if manifest.get('schema') != 2 or manifest.get('python_abi') != 'cp310':
        raise prep.Stop('Web bundle requires the verified Linux Python 3.10 ABI')
    rows, seen = manifest.get('files', []), set()
    if not 20 < len(rows) <= 500:
        raise prep.Stop('Invalid web manifest')
    for row in rows:
        name = row.get('path', '')
        path = PurePosixPath(name)
        if not re.fullmatch(r'[A-Za-z0-9_.+/-]+', name) or path.is_absolute() or '..' in path.parts or name in seen:
            raise prep.Stop('Unsafe web path')
        seen.add(name)
        file = bundle / name
        if any(p.is_symlink() for p in [file] + list(file.parents)[:len(path.parts)]):
            raise prep.Stop('Symlink bundle refused')
        if not file.is_file() or file.stat().st_size != row['size'] or prep.digest(file) != row['sha256']:
            raise prep.Stop('Web file checksum mismatch: ' + name)
    if not {'app/court_transcript/app.py', 'app/static/index.html', 'assets/ffmpeg.tar.xz', 'private/access-seeds.json'} <= seen:
        raise prep.Stop('Incomplete web bundle')
    return manifest


def extract_ffmpeg(archive, target):
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)
    with tarfile.open(archive, 'r:xz') as tar:
        for name in ['ffmpeg', 'ffprobe']:
            rows = [m for m in tar.getmembers() if m.isfile() and m.name.endswith('/bin/' + name)]
            if len(rows) != 1 or rows[0].size > 512 * 1024**2:
                raise prep.Stop('Unexpected FFmpeg build layout')
            file = target / name
            if file.is_symlink():
                raise prep.Stop('Symlink binary refused')
            with tar.extractfile(rows[0]) as source, file.open('wb') as output:
                shutil.copyfileobj(source, output)
            file.chmod(0o755)


def unit_text(root, release, python, listen, port, uid, gid, asr_env):
    return f'''[Unit]
Description={DESCRIPTION}
After=network.target
[Service]
Type=simple
User={uid}
Group={gid}
WorkingDirectory={release}
EnvironmentFile={root}/runtime.env
ExecStart={python} -m uvicorn court_transcript.app:app --host {listen} --port {port} --workers 1 --no-proxy-headers --limit-concurrency 32 --timeout-keep-alive 5
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=20
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=tmpfs
InaccessiblePaths=-/run/docker.sock -/run/containerd/containerd.sock -/run/podman/podman.sock
BindReadOnlyPaths={release} {python.parent.parent} {asr_env} {root.parent}/court-transcript-runtime-v1/models {root}/bin
BindPaths={root}/data {root}/reports
ReadWritePaths={root}/data {root}/reports
MemoryMax=16G
CPUQuota=800%
TasksMax=256
UMask=0077
[Install]
WantedBy=multi-user.target
'''


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None

    def request(self, path, data=None, content_type='application/json', method=None):
        headers = {'Origin': self.base}
        if data is not None:
            headers['Content-Type'] = content_type
        if self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw

    def login(self, credential):
        self.request('/api/auth/login', json.dumps({'credential': credential}).encode())
        self.csrf = self.request('/api/auth/session')['csrf']


def loopback_smoke(root, release, python, env, asr, access, uid, gid, *, device='cuda', external_base=None):
    one, two = access.add_user('工程验收 A'), access.add_user('工程验收 B')
    env = dict(env, COURT_DEVICE=device)
    process = None
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        video = root / 'reports/control-video.mkv'
        subprocess.run([str(root / 'bin/ffmpeg'), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=gray:s=320x180:r=1', '-i', str(asr / 'fixtures/jfk.flac'), '-shortest',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(video)], check=True, timeout=60)
        os.chown(video, uid, gid)
        with (root / 'reports/loopback-server.log').open('ab') as log:
            os.fchown(log.fileno(), uid, gid)
            if not external_base:
                process = subprocess.Popen([str(python), '-m', 'uvicorn', 'court_transcript.app:app', '--host', '127.0.0.1',
                                            '--port', str(port), '--no-proxy-headers'], cwd=release, env=env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, user=uid, group=gid,
                                           extra_groups=os.getgrouplist(__import__('pwd').getpwuid(uid).pw_name, gid), umask=0o077)
            client = Client(external_base or 'http://127.0.0.1:' + str(port))
            for _ in range(80):
                if process and process.poll() is not None:
                    raise prep.Stop('Loopback web process exited; see private log')
                try:
                    client.request('/api/auth/session')
                    break
                except (urllib.error.URLError, OSError):
                    time.sleep(.25)
            else:
                raise prep.Stop('Loopback health check timed out')
            client.login(one['credential'])
            boundary = 'CourtControl' + os.urandom(8).hex()
            body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="options"\r\n\r\n' +
                    '{"diarize":false,"language":"en"}\r\n--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="control.mkv"\r\nContent-Type: video/x-matroska\r\n\r\n').encode() + video.read_bytes() + ('\r\n--' + boundary + '--\r\n').encode()
            task = client.request('/api/tasks', body, 'multipart/form-data; boundary=' + boundary)
            task_id = task['id']
            for _ in range(600):
                task = client.request('/api/tasks/' + task_id)
                if task['state'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
                    break
                time.sleep(.5)
            if task['state'] != 'completed':
                raise prep.Stop('Real loopback ASR workflow did not complete; inspect private task log')
            markdown = client.request('/api/tasks/' + task_id + '/export/markdown')
            if b'country' not in markdown.lower():
                raise prep.Stop('Control transcription did not contain its expected words')
            (root / 'reports/control-transcript.md').write_bytes(markdown)
            os.chown(root / 'reports/control-transcript.md', uid, gid)
            second = Client(client.base)
            second.login(two['credential'])
            if second.request('/api/tasks'):
                raise prep.Stop('Cross-user task list isolation failed')
            try:
                second.request('/api/tasks/' + task_id + '/export/markdown')
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise prep.Stop('Cross-user export denial failed')
            else:
                raise prep.Stop('Cross-user export was exposed')
            result = {'status': 'passed', 'device': device, 'task_id': task_id, 'upload': 'real synthetic video',
                      'transcript_words_checked': 'country', 'cross_user_denial': 'passed', 'worker_subprocess': True}
            print('COURT_WEB_LOOPBACK_E2E_OK', flush=True)
            return result
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        access.revoke_user(one['id'])
        access.revoke_user(two['id'])


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--listen', required=True)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--gpu', type=int, default=3)
    parser.add_argument('--allow-campus-http', action='store_true')
    args = parser.parse_args()
    uid, gid, home = prep.identity()
    if sys.version_info[:2] != (3, 10):
        raise prep.Stop('Use the verified server Python 3.10')
    address = ipaddress.IPv4Address(args.listen)
    if not address.is_private or address.is_loopback or address.is_unspecified or not 1024 <= args.port <= 65535 or not args.allow_campus_http:
        raise prep.Stop('Use an explicit local campus IPv4/port and --allow-campus-http test mode')
    interfaces = json.loads(prep.run(['ip', '-j', 'address']).stdout)
    if args.listen not in [a.get('local') for interface in interfaces for a in interface.get('addr_info', [])]:
        raise prep.Stop('Listen address is not a current local interface')
    bundle = args.bundle.resolve()
    if not bundle.is_relative_to(home) or args.bundle.is_symlink():
        raise prep.Stop('Web bundle must be inside the invoking account home')
    print('STAGE verify_web_bundle', flush=True)
    manifest = verify_bundle(bundle)
    fingerprint = prep.digest(bundle / 'manifest.json')
    root = home / 'court-transcript-web-v1'
    marker = root / 'owner.json'
    if root.exists() or root.is_symlink():
        if root.is_symlink() or marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != {'profile': PROFILE, 'uid': uid}:
            raise prep.Stop('Unknown existing web directory; not modified')
    else:
        root.mkdir(mode=0o700)
        marker.write_text(json.dumps({'profile': PROFILE, 'uid': uid}))
    os.chown(root, uid, gid)
    lock_path = root / 'install.lock'
    if lock_path.is_symlink():
        raise prep.Stop('Symlink install lock refused')
    lock = lock_path.open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    status = {'status': 'preparing', 'version': manifest['version'], 'bundle_manifest_sha256': fingerprint, 'other_services_changed': False,
              'chinese_court_quality_validated': False, 'campus_peer_reachability': 'not_verified'}
    def stage(value):
        status['stage'] = value
        prep.write_owned(root / 'status.json', json.dumps(status, ensure_ascii=False, indent=2), uid, gid)
        print('STAGE ' + value, flush=True)
    unit_path = Path('/etc/systemd/system') / UNIT
    started = False
    try:
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)):
            raise prep.Stop('Server home path is not supported by this service profile')
        if unit_path.is_symlink() or (unit_path.exists() and ('Description=' + DESCRIPTION + '\n') not in unit_path.read_text()):
            raise prep.Stop('Existing systemd unit is not owned by this tool')
        if prep.run(['systemctl', 'is-active', UNIT], check=False).returncode == 0:
            raise prep.Stop('Web service is already active; do not reinstall or interrupt its tasks')
        with socket.socket() as sock:
            sock.bind((args.listen, args.port))
        if shutil.disk_usage(root).free < 20 * 1024**3:
            raise prep.Stop('Keep 20 GiB server disk reserve')
        stage('reuse_verified_asr_environment')
        asr = home / 'court-transcript-runtime-v1'
        report = json.loads((asr / 'status.json').read_text())
        if report.get('status') != 'passed' or report.get('mode') != 'offline-venv':
            raise prep.Stop('Verified ASR preparation is required; not rebuilding it')
        old_envs = [p for p in (asr / 'venvs').iterdir() if (p / 'court-owner.json').is_file() and p.name == 'cp310-' + report['bundle_manifest_sha256'][:12]]
        if len(old_envs) != 1:
            raise prep.Stop('Verified ASR venv not found')
        if json.loads((old_envs[0] / 'court-owner.json').read_text()) != {'profile': prep.PROFILE, 'manifest_sha256': report['bundle_manifest_sha256'], 'uid': uid}:
            raise prep.Stop('ASR venv ownership record differs')
        for name, size, sha in prep.FILES:
            file = asr / 'models' / name
            if not file.is_file() or file.stat().st_size != size or prep.digest(file) != sha:
                raise prep.Stop('Existing ASR model differs; not replaced or downloaded')
        old_site = old_envs[0] / 'lib/python3.10/site-packages'
        gpu = prep.gpu_info(args.gpu)
        prep.require_idle_gpu(gpu)
        for name in ['data', 'reports', 'releases', 'venvs']:
            directory = root / name
            if directory.is_symlink():
                raise prep.Stop('Symlink runtime directory refused')
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chown(directory, uid, gid)
        upload_tmp = root / 'data/upload-tmp'
        if upload_tmp.is_symlink():
            raise prep.Stop('Symlink temporary upload directory refused')
        upload_tmp.mkdir(mode=0o700, exist_ok=True)
        os.chown(upload_tmp, uid, gid)
        release = root / 'releases' / fingerprint[:16]
        release_marker = release / 'court-release.json'
        release_identity = {'profile': PROFILE, 'manifest_sha256': fingerprint, 'uid': uid}
        if release.exists():
            if release.is_symlink() or release_marker.is_symlink() or not release_marker.is_file() or json.loads(release_marker.read_text()) != release_identity:
                raise prep.Stop('Unknown existing release; not modified')
            for row in manifest['files']:
                if row['path'].startswith('app/'):
                    existing = release / row['path'][4:]
                    if existing.is_symlink() or not existing.is_file() or prep.digest(existing) != row['sha256']:
                        raise prep.Stop('Previous release differs; preserve it for inspection')
        else:
            shutil.copytree(bundle / 'app', release)
            release_marker.write_text(json.dumps(release_identity))
        release.chmod(0o755)
        # Read-only application files. Service writes only data/reports and its private /tmp.
        for file in release.rglob('*'):
            file.chmod(0o755 if file.is_dir() else 0o644)
        extract_ffmpeg(bundle / 'assets/ffmpeg.tar.xz', root / 'bin')
        python_dir = root / 'venvs' / fingerprint[:16]
        stage('install_offline_web_venv')
        env_marker = python_dir / 'court-web-venv.json'
        if python_dir.exists() and (python_dir.is_symlink() or env_marker.is_symlink() or not env_marker.is_file() or json.loads(env_marker.read_text()) != release_identity):
            raise prep.Stop('Unknown existing web venv; not modified')
        venv.EnvBuilder(with_pip=False).create(python_dir)
        env_marker.write_text(json.dumps(release_identity))
        python = python_dir / 'bin/python3'
        site = python_dir / 'lib/python3.10/site-packages'
        (site / 'verified-asr.pth').write_text(str(old_site) + '\n')
        wheels = sorted((bundle / 'wheels').glob('*.whl'))
        pip = next(p for p in wheels if p.name.startswith('pip-'))
        install_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONPATH': str(pip), 'HOME': str(home), 'PYTHONNOUSERSITE': '1'}
        subprocess.run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install', '--no-index', '--no-deps',
                        '--no-cache-dir', '--no-compile', '--ignore-installed'] + list(map(str, wheels)), env=install_env, check=True, timeout=600)
        subprocess.run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-cache-dir', 'check'], env=install_env, check=True, timeout=60)
        # sudo may inherit umask 077; the unprivileged service must read this isolated venv.
        python_dir.chmod(0o755)
        for file in python_dir.rglob('*'):
            if not file.is_symlink():
                file.chmod(0o755 if file.is_dir() or file.parent == python_dir / 'bin' else 0o644)
        for binary in ['ffmpeg', 'ffprobe']:
            subprocess.run([str(root / 'bin' / binary), '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)
        spec = importlib.util.spec_from_file_location('court_web_access', release / 'court_transcript/access.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        access = module.Access(root / 'data/access.sqlite')
        for record in json.loads((bundle / 'private/access-seeds.json').read_text('utf-8')):
            access.seed_user(record)
        os.chown(access.path, uid, gid)
        env = {'PATH': str(root / 'bin') + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(home),
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1',
               'COURT_DATA_DIR': str(root / 'data/tasks'), 'COURT_ACCESS_DB': str(access.path),
               'TMPDIR': str(upload_tmp),
               'COURT_ALLOW_CAMPUS_HTTP': '1', 'COURT_ALLOWED_HOSTS': args.listen + ',127.0.0.1,localhost',
               'COURT_ASR_MODEL': str(asr / 'models'), 'COURT_DEVICE': 'cuda', 'COURT_COMPUTE_TYPE': 'int8_float16',
               'COURT_ALLOW_MODEL_DOWNLOAD': '0', 'COURT_WORKER_SUBPROCESS': '1', 'COURT_GPU_UUID': gpu['uuid'],
               'CUDA_VISIBLE_DEVICES': gpu['uuid'], 'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
               'HF_HUB_DISABLE_IMPLICIT_TOKEN': '1', 'XDG_CACHE_HOME': '/tmp/court-cache', 'CUDA_CACHE_PATH': '/tmp/court-cuda-cache',
               'LD_LIBRARY_PATH': str(old_site / 'nvidia/cublas/lib') + ':' + str(old_site / 'nvidia/cudnn/lib')}
        # Prove the workflow inside the actual systemd sandbox before the campus listener exists.
        prep.write_owned(root / 'runtime.env', '\n'.join(k + '=' + v for k, v in env.items()) + '\n', uid, gid)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            probe_port = sock.getsockname()[1]
        unit_path.write_text(unit_text(root, release, python, '127.0.0.1', probe_port, uid, gid, old_envs[0]))
        unit_path.chmod(0o644)
        prep.run(['systemctl', 'daemon-reload'])
        started = True
        prep.run(['systemctl', 'start', UNIT])
        stage('systemd_loopback_gpu_workflow')
        status['loopback_e2e'] = loopback_smoke(root, release, python, env, asr, access, uid, gid, external_base='http://127.0.0.1:' + str(probe_port))
        prep.run(['systemctl', 'stop', UNIT])
        time.sleep(2)
        after = prep.gpu_info(args.gpu)
        if after['used_mib'] > gpu['used_mib'] + 256:
            raise prep.Stop('GPU memory was not released after the task process; inspect before starting campus listener')
        status['gpu_before'], status['gpu_after'] = gpu, after
        # Only now enable the explicitly requested campus listener.
        stage('start_campus_listener')
        unit_path.write_text(unit_text(root, release, python, args.listen, args.port, uid, gid, old_envs[0]))
        unit_path.chmod(0o644)
        prep.run(['systemctl', 'daemon-reload'])
        started = True
        prep.run(['systemctl', 'enable', '--now', UNIT])
        url = 'http://' + args.listen + ':' + str(args.port)
        for _ in range(40):
            try:
                active = prep.run(['systemctl', 'is-active', UNIT], check=False).returncode == 0
                if active:
                    session = Client(url).request('/api/auth/session')
                    if session.get('required') is True and session.get('authenticated') is False:
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(.5)
        else:
            raise prep.Stop('Campus listener did not start')
        status.update(status='ready', url=url, loopback_verified=True, campus_listener=True)
        print('COURT_CAMPUS_WEB_READY', flush=True)
        print('URL=' + url, flush=True)
        print('CREDENTIALS=use the private file already generated on your computer; do not paste it into chat', flush=True)
        print('GLM_NOT_CALLED; FIREWALL_NOT_CHANGED; CAMPUS_PEER_ACCESS_NOT_YET_VERIFIED', flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        status.update(status='failed', error=str(exc) if isinstance(exc, prep.Stop) else type(exc).__name__)
        if started:
            diagnostics = prep.run(['journalctl', '-u', UNIT, '-n', '60', '--no-pager'], check=False).stdout
            prep.write_owned(root / 'reports/service-diagnostics.log', diagnostics, uid, gid)
            description = prep.run(['systemctl', 'show', UNIT, '--property=Description', '--value'], check=False).stdout.strip()
            if description == DESCRIPTION:
                prep.run(['systemctl', 'stop', UNIT], check=False)
        raise
    finally:
        prep.write_owned(root / 'status.json', json.dumps(status, ensure_ascii=False, indent=2), uid, gid)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('COURT_WEB_DEPLOY_STOPPED: ' + (str(exc) if isinstance(exc, prep.Stop) else type(exc).__name__), flush=True)
        raise SystemExit(1)
