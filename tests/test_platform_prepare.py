import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile

import pytest

from tools.build_platform_bundle import server_commands


def test_preparation_command_uses_uploaded_hash_and_explicit_campus_account():
    directory = str(PurePosixPath('/home') / 'synthetic' / ('court-asr-upload.' + 'a' * 12))
    block = server_commands(directory, 'b' * 64, 'synthetic')
    assert 'sha256sum --check -' in block and 'campus_platform_prepare.py' in block
    assert "--account 'synthetic'" in block
    for wrong in ('$(unsafe)', '/tmp/unknown'):
        with pytest.raises(ValueError):
            server_commands(wrong, 'b' * 64, 'synthetic')


@pytest.fixture
def native_bundle(tmp_path):
    if sys.platform != 'linux' or not shutil.which('openssl'):
        pytest.skip('Linux stdlib account and file protections required')
    from tools import campus_platform_prepare as prep
    from tools import build_platform_bundle as builder
    private, public = tmp_path / 'fixture.pem', tmp_path / 'fixture.pub'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'Ed25519', '-out', str(private)], check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps({'campus': {'app_sha256': 'a' * 64, 'access_sha256': 'b' * 64}}))
    receipt = builder.build(public, baseline, tmp_path / 'output')
    extracted = tmp_path / 'payload'
    extracted.mkdir()
    with tarfile.open(receipt['archive']) as archive:
        # Locally built members are independently validated before use below.
        for row in archive.getmembers():
            assert row.isfile() and not Path(row.name).is_absolute() and '..' not in Path(row.name).parts
            path = extracted / row.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(row).read())
    manifest, fingerprint = prep.verify_bundle(extracted)
    return prep, builder, extracted, manifest, fingerprint, receipt


def test_preparation_bundle_excludes_private_keys_models_and_font_bytes(native_bundle):
    prep, builder, payload, manifest, fingerprint, receipt = native_bundle
    assert receipt['private_keys_included'] is False and receipt['models_or_wheels_included'] is False
    assert receipt['size'] < 1024 * 1024
    assert not list(payload.rglob('*.woff2')) and not list(payload.rglob('*.key'))
    assert not (payload / '.env').exists()
    (payload / 'app/court_transcript/bridge.py').write_bytes(b'changed')
    with pytest.raises(prep.Refusal, match='checksum'):
        prep.verify_bundle(payload)


def test_native_preparation_generates_key_once_and_preserves_live_baseline(native_bundle, tmp_path):
    prep, builder, payload, manifest, fingerprint, receipt = native_bundle
    parent = tmp_path / 'root-private'
    live = tmp_path / 'live-runtime.env'
    live.write_text('UNCHANGED_SYNTHETIC=1\n')
    observed = {'unit_sha256': 'c' * 64, 'runtime_env_sha256': hashlib.sha256(live.read_bytes()).hexdigest()}
    commands = []
    def run(args):
        commands.append(args)
        return prep.command(args)
    first = prep.prepare(payload, manifest, fingerprint, observed, parent=parent, run=run)
    second = prep.prepare(payload, manifest, fingerprint, observed, parent=parent, run=run)
    assert first == second and len(commands) == 2 and commands[0][1] == 'req'
    assert commands[1][0].endswith('ssh-keygen') and first['ssh_public_key'].startswith('ssh-ed25519 ')
    assert first['activation_performed'] is False and first['services_changed'] is False
    assert 'private_key_file_sha256' not in first
    target = Path(first['preparation_directory'])
    assert (target / 'server.key').stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE KEY' not in json.dumps(first)
    assert live.read_text() == 'UNCHANGED_SYNTHETIC=1\n'
    (target / 'server.crt').write_text('changed')
    with pytest.raises(prep.Refusal, match='certificate'):
        prep.prepare(payload, manifest, fingerprint, observed, parent=parent, run=run)
    assert len(commands) == 2


def test_changed_live_baseline_or_partial_preparation_never_regenerates(native_bundle, tmp_path):
    prep, builder, payload, manifest, fingerprint, receipt = native_bundle
    parent = tmp_path / 'root-private'
    observed = {'unit_sha256': 'a' * 64}
    prep.prepare(payload, manifest, fingerprint, observed, parent=parent)
    with pytest.raises(prep.Refusal, match='baseline changed'):
        prep.prepare(payload, manifest, fingerprint, {'unit_sha256': 'b' * 64}, parent=parent,
                     run=lambda args: pytest.fail('no repeated command'))
    (parent / fingerprint[:20] / 'result.json').unlink()
    with pytest.raises(FileNotFoundError):
        prep.prepare(payload, manifest, fingerprint, observed, parent=parent,
                     run=lambda args: pytest.fail('no repeated command'))


def test_baseline_source_change_stops_before_service_command(native_bundle, tmp_path):
    prep, builder, payload, manifest, fingerprint, receipt = native_bundle
    home = tmp_path / 'home/synthetic'
    root = home / 'court-transcript-web-v1'
    release = root / 'releases/previous'
    (release / 'court_transcript').mkdir(parents=True)
    (release / 'court_transcript/app.py').write_bytes(b'changed')
    (root / 'owner.json').write_text(json.dumps({'profile': 'court-transcript-campus-web-v1', 'uid': os.getuid()}))
    unit = tmp_path / 'unit.service'
    unit.write_text('Description=Court transcript campus web v1\nWorkingDirectory=' + str(release) + '\n')
    with pytest.raises(prep.Refusal, match='live source changed'):
        prep.baseline(home, os.getuid(), unit, manifest, run=lambda args: pytest.fail('no command on mismatched source'))
