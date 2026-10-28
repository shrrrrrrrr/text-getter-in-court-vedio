import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.ssh_bundle_upload import hash_prefix, read_record, remote_command, send

ROOT = Path(__file__).resolve().parents[1]
LINUX = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux remote filesystem and flock')


def test_remote_command_survives_windows_argument_quoting():
    command = remote_command('/safe/bundle.tar', 'a' * 64, 99)
    assert '"' not in command
    words = shlex.split(command)
    assert words[:2] == ['python3', '-c']
    compile(base64.b64decode(words[3]), '<remote-uploader>', 'exec')
    assert words[4:] == ['--receive', '/safe/bundle.tar', 'a' * 64, '99']
    with pytest.raises(RuntimeError, match='Unsafe'):
        remote_command('/safe/$(command)', 'a' * 64, 99)


@pytest.mark.parametrize('raw', [b'', b'[]\n', b'x' * 4097])
def test_broken_ssh_output_stops_without_installation(raw):
    with pytest.raises((RuntimeError, ValueError)):
        read_record(io.BytesIO(raw))


def test_sender_rejects_different_prefix_without_sending_archive(tmp_path):
    archive = tmp_path / 'local.tar'
    archive.write_bytes(b'good bytes')
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(
        (json.dumps({'offset': 4, 'prefix_sha256': hashlib.sha256(b'evil').hexdigest()}) + '\n').encode()))
    with pytest.raises(RuntimeError, match='prefix differs'):
        send(process, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert process.stdin.getvalue() == b'ABORT\n'


def receiver_process(tmp_path, content, prefix=b''):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    directory = home / 'court-asr-upload.012345abcdef'
    directory.mkdir(mode=0o700, exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    path = directory / ('bundle-' + sha + '.tar')
    if prefix:
        path.write_bytes(prefix)
        path.chmod(0o600)
    command = shlex.split(remote_command(str(path), sha, len(content)))
    command[0] = sys.executable
    environment = dict(os.environ, HOME=str(home))
    process = subprocess.Popen(command, env=environment, cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process, path, sha


def finish(process):
    if not process.stdin.closed:
        process.stdin.close()
    process.wait(timeout=15)
    errors = process.stderr.read().decode()
    process.stdout.close()
    process.stderr.close()
    return errors


@LINUX
@pytest.mark.parametrize('prefix_size', [0, 17003, 10 * 1024 * 1024])
def test_real_duplex_binary_upload_and_resume(tmp_path, prefix_size):
    content = bytes(range(256)) * 40960
    local = tmp_path / 'local.tar'
    local.write_bytes(content)
    process, remote, sha = receiver_process(tmp_path, content, content[:prefix_size])
    messages = []
    try:
        send(process, local, sha, messages.append)
        assert finish(process) == ''
        assert remote.read_bytes() == content
        assert messages[0].startswith('SSH_UPLOAD_RESUME ' + str(prefix_size) + '/')
        assert messages[-1] == 'SSH_UPLOAD_VERIFIED'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@LINUX
def test_disconnect_preserves_prefix_then_real_resume(tmp_path):
    content = b'\x00\xff\x80\n\r' * 20000
    local = tmp_path / 'local.tar'
    local.write_bytes(content)
    process, remote, sha = receiver_process(tmp_path, content)
    assert read_record(process.stdout)['offset'] == 0
    process.stdin.write(b'GO\n' + content[:12345])
    process.stdin.close()
    assert 'partial archive retained' in finish(process)
    assert process.returncode != 0 and remote.read_bytes() == content[:12345]
    process, remote, sha = receiver_process(tmp_path, content)
    send(process, local, sha, lambda msg: None)
    assert finish(process) == '' and remote.read_bytes() == content


@LINUX
def test_receiver_corrupt_full_archive_never_reports_verified(tmp_path):
    content = b'correct'
    process, remote, sha = receiver_process(tmp_path, content, b'corrupt')
    read_record(process.stdout)
    process.stdin.write(b'GO\n')
    process.stdin.close()
    assert process.stdout.read() == b''
    assert 'SHA256 mismatch' in finish(process)
    assert process.returncode != 0 and remote.read_bytes() == b'corrupt'


@LINUX
def test_receiver_symlink_does_not_modify_target(tmp_path):
    from tools.ssh_bundle_upload import receive
    content = b'original'
    process, remote, sha = receiver_process(tmp_path, content)
    read_record(process.stdout)
    process.stdin.close()
    finish(process)
    remote.unlink()
    other = tmp_path / 'keep'
    other.write_bytes(content)
    remote.symlink_to(other)
    with pytest.raises(OSError):
        receive(remote, sha, len(content), io.BytesIO(b'GO\nchanged!'), io.BytesIO(), remote.parent.parent)
    assert other.read_bytes() == content


@LINUX
def test_real_mismatched_prefix_is_preserved(tmp_path):
    content = b'correct archive'
    local = tmp_path / 'local.tar'
    local.write_bytes(content)
    process, remote, sha = receiver_process(tmp_path, content, b'wrong')
    with pytest.raises(RuntimeError, match='prefix differs'):
        send(process, local, sha)
    assert 'did not confirm matching prefix' in finish(process)
    assert remote.read_bytes() == b'wrong'


@LINUX
def test_concurrent_upload_cannot_write_locked_archive(tmp_path):
    content = b'archive'
    first, remote, sha = receiver_process(tmp_path, content)
    read_record(first.stdout)
    try:
        second, _, _ = receiver_process(tmp_path, content)
        assert second.stdout.read() == b''
        assert 'SSH_UPLOAD_STOPPED' in finish(second)
        assert second.returncode != 0
    finally:
        first.stdin.close()
        finish(first)
    assert remote.read_bytes() == b''


def test_hash_prefix_bounds():
    assert hash_prefix(io.BytesIO(b'abc'), 2).hexdigest() == hashlib.sha256(b'ab').hexdigest()
    with pytest.raises(RuntimeError, match='EOF'):
        hash_prefix(io.BytesIO(b'a'), 2)
