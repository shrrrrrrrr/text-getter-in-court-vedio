import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from tools.build_campus_web import server_commands
from tools.campus_web_install import extract_ffmpeg, unit_text, verify_bundle
from tools.campus_prepare import Stop


def test_unit_is_one_service_without_model_platform_control(tmp_path):
    text = unit_text(tmp_path, tmp_path / 'release', tmp_path / 'venv/bin/python', '10.20.30.40', 8766, 1001, 1001, tmp_path / 'verified-asr-env')
    for item in ['--workers 1', '--no-proxy-headers', 'KillMode=control-group', 'ProtectSystem=strict', 'ProtectHome=tmpfs', 'BindReadOnlyPaths=', 'BindPaths=', 'MemoryMax=16G', 'User=1001']:
        assert item in text
    assert 'InaccessiblePaths=-/run/docker.sock' in text
    assert 'docker run' not in text and 'sudo' not in text and 'GLM' not in text


def test_continuation_uses_existing_packet_and_explicit_http_mode():
    text = server_commands('a' * 12, 'b' * 64, '10.20.30.40')
    assert 'sha256sum --check -' in text and '--allow-campus-http' in text
    assert 'curl' not in text and 'pip' not in text and 'campus_web_install.py' in text
    with pytest.raises(ValueError):
        server_commands('a' * 12, 'b' * 64, '0.0.0.0')
    with pytest.raises(ValueError):
        server_commands('$(run)', 'b' * 64, '10.20.30.40')


def test_only_known_ffmpeg_regular_binary_members_are_extracted(tmp_path):
    archive = tmp_path / 'ffmpeg.tar.xz'
    with tarfile.open(archive, 'w:xz') as tar:
        for name in ['build/bin/ffmpeg', 'build/bin/ffprobe', '../../outside']:
            item = tarfile.TarInfo(name)
            item.size = 3
            tar.addfile(item, io.BytesIO(b'abc'))
    dest = tmp_path / 'bin'
    extract_ffmpeg(archive, dest)
    assert sorted(p.name for p in dest.iterdir()) == ['ffmpeg', 'ffprobe']
    assert not (tmp_path / 'outside').exists()


def test_web_manifest_rejects_corruption_and_path_escape(tmp_path):
    required = ['app/court_transcript/app.py', 'app/static/index.html', 'assets/ffmpeg.tar.xz', 'private/access-seeds.json']
    paths = required + ['extra-' + str(n) for n in range(20)]
    rows = []
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'abc')
        rows.append({'path': name, 'size': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'schema': 2, 'python_abi': 'cp310', 'files': rows}))
    assert verify_bundle(tmp_path)['schema'] == 2
    (tmp_path / required[0]).write_bytes(b'bad')
    with pytest.raises(Stop, match='checksum'):
        verify_bundle(tmp_path)
    (tmp_path / required[0]).write_bytes(b'abc')
    rows[0]['path'] = '../../escape'
    manifest.write_text(json.dumps({'schema': 2, 'python_abi': 'cp310', 'files': rows}))
    with pytest.raises(Stop, match='Unsafe'):
        verify_bundle(tmp_path)
