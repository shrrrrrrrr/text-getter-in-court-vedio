import hashlib
import io
import os
import subprocess
import sys
import tarfile

import pytest

from tools.build_campus_offline import server_continue_command


@pytest.mark.parametrize('upload_id,sha', [
    ('../outside', 'a' * 64), ('a' * 12, '$(command)'), ('a' * 13, 'a' * 64),
])
def test_continuation_refuses_shell_injection(upload_id, sha):
    with pytest.raises(RuntimeError, match='unsafe server continuation'):
        server_continue_command(upload_id, sha)


@pytest.mark.skipif(sys.platform != 'linux', reason='real Bash and GNU tar continuation')
@pytest.mark.parametrize('corrupted', [False, True])
def test_existing_archive_continues_only_after_sha_check(tmp_path, corrupted):
    home = tmp_path / 'home'
    directory = home / 'court-asr-upload.012345abcdef'
    directory.mkdir(parents=True)
    archive = directory / 'original.tar'
    source = b'''import sys
from pathlib import Path
assert sys.argv[-2:] == ['--gpu', '3']
assert Path(sys.argv[sys.argv.index('--bundle')+1]).is_dir()
print('SYNTHETIC_CONTINUATION_PASSED')
'''
    with tarfile.open(archive, 'w') as tar:
        item = tarfile.TarInfo('tools/campus_offline.py')
        item.size, item.mode = len(source), 0o600
        tar.addfile(item, io.BytesIO(source))
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    uploaded = directory / ('bundle-' + sha + '.tar')
    archive.rename(uploaded)
    if corrupted:
        uploaded.write_bytes(b'corrupted bytes')
    executable_dir = tmp_path / 'bin'
    executable_dir.mkdir()
    sudo = executable_dir / 'sudo'
    sudo.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$COURT_TEST_ARGS"\nexec "$@"\n', 'utf-8')
    sudo.chmod(0o700)
    calls = tmp_path / 'sudo-calls'
    env = dict(os.environ, HOME=str(home), PATH=str(executable_dir) + ':' + os.environ['PATH'], COURT_TEST_ARGS=str(calls))
    script = server_continue_command('012345abcdef', sha)
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=30)
    if corrupted:
        assert result.returncode != 0
        assert not calls.exists() and not list(directory.glob('payload.*'))
    else:
        assert result.returncode == 0, result.stderr
        assert 'SYNTHETIC_CONTINUATION_PASSED' in result.stdout
        assert calls.read_text().splitlines()[0] == 'python3'
        assert calls.read_text().splitlines()[-2:] == ['--gpu', '3']
        assert len(list(directory.glob('payload.*'))) == 1
        assert hashlib.sha256(uploaded.read_bytes()).hexdigest() == sha
