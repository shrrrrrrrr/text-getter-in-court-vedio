"""Resume a verified archive over ordinary SSH, without SCP/SFTP or remote downloads."""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

BLOCK = 4 * 1024 * 1024
SSH_OPTIONS = ['-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6']


def hash_prefix(stream, size):
    digest = hashlib.sha256()
    stream.seek(0)
    remaining = size
    while remaining:
        block = stream.read(min(BLOCK, remaining))
        if not block:
            raise RuntimeError('Unexpected EOF while verifying archive prefix')
        digest.update(block)
        remaining -= len(block)
    return digest


def emit(output, record):
    output.write((json.dumps(record) + '\n').encode('ascii'))
    output.flush()


def read_record(stream):
    line = stream.readline(4097)
    if not line or len(line) > 4096:
        raise RuntimeError('SSH upload protocol interrupted; retain partial archive and rerun')
    record = json.loads(line)
    if not isinstance(record, dict):
        raise RuntimeError('Invalid upload response')
    return record


def receive(path, expected, size, source=None, output=None, home=None):
    """Remote standard-library receiver; a disconnect keeps only the received prefix."""
    import fcntl
    source = source or sys.stdin.buffer
    output = output or sys.stdout.buffer
    home = (home or Path.home()).resolve()
    if not re.fullmatch('[a-f0-9]{64}', expected) or not 0 < size <= 16 * 1024**3:
        raise RuntimeError('Invalid archive identity')
    path = Path(path)
    parent = path.parent
    if (not path.is_absolute() or parent.resolve() != parent or
            parent.parent != home or not re.fullmatch(r'court-asr-upload\.[a-f0-9]{12}', parent.name) or
            path.name != 'bundle-' + expected + '.tar'):
        raise RuntimeError('Unsafe upload path')
    directory = parent.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077:
        raise RuntimeError('Upload directory must be private and owned by this account')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+b') as dest:
        fcntl.flock(dest, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(dest.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeError('Unsafe existing archive; not overwritten')
        offset = info.st_size
        if offset > size:
            raise RuntimeError('Existing archive is oversized; not overwritten')
        digest = hash_prefix(dest, offset)
        emit(output, {'offset': offset, 'prefix_sha256': digest.hexdigest()})
        if source.readline(16) != b'GO\n':
            raise RuntimeError('Sender did not confirm matching prefix; archive preserved')
        try:
            while offset < size:
                block = source.read(min(BLOCK, size - offset))
                if not block:
                    raise RuntimeError('Upload disconnected; partial archive retained for resume')
                dest.write(block)
                digest.update(block)
                offset += len(block)
        finally:
            dest.flush()
            os.fsync(dest.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError('Uploaded archive SHA256 mismatch; installer must not run')
        emit(output, {'status': 'UPLOAD_VERIFIED', 'size': offset, 'sha256': expected})


def send(process, archive, expected, progress=print):
    """Duplex protocol shared by the SSH client and real subprocess regression tests."""
    size = archive.stat().st_size
    start = read_record(process.stdout)
    offset = start.get('offset')
    if type(offset) is not int or not 0 <= offset <= size:
        raise RuntimeError('Invalid remote resume offset')
    with archive.open('rb') as src:
        prefix = hash_prefix(src, offset).hexdigest()
        if prefix != start.get('prefix_sha256'):
            process.stdin.write(b'ABORT\n')
            process.stdin.flush()
            raise RuntimeError('Remote prefix differs; existing file preserved, no installation')
        progress('SSH_UPLOAD_RESUME ' + str(offset) + '/' + str(size) + ' bytes')
        process.stdin.write(b'GO\n')
        process.stdin.flush()
        written, last = offset, time.monotonic()
        while written < size:
            block = src.read(min(BLOCK, size - written))
            if not block:
                raise RuntimeError('Local archive changed during upload')
            process.stdin.write(block)
            written += len(block)
            if time.monotonic() - last >= 5:
                progress('SSH_UPLOAD ' + str(round(100 * written / size, 1)) + '%')
                last = time.monotonic()
        process.stdin.close()
    result = read_record(process.stdout)
    if result != {'status': 'UPLOAD_VERIFIED', 'size': size, 'sha256': expected}:
        raise RuntimeError('Remote archive verification failed; installer must not run')
    if process.wait(timeout=60) != 0:
        raise RuntimeError('SSH upload exited unsuccessfully; archive retained')
    progress('SSH_UPLOAD_VERIFIED')


def remote_command(path, expected, size):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise RuntimeError('Unsafe remote arguments')
    encoded = base64.b64encode(Path(__file__).read_bytes()).decode('ascii')
    return ("python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))' " + encoded +
            ' --receive ' + path + ' ' + expected + ' ' + str(int(size)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receive', nargs=3, metavar=('PATH', 'SHA256', 'SIZE'))
    parser.add_argument('--target')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--remote-path')
    args = parser.parse_args()
    if args.receive:
        receive(args.receive[0], args.receive[1], int(args.receive[2]))
        return
    if not args.target or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*', args.target):
        raise RuntimeError('Invalid SSH target')
    with args.archive.open('rb') as src:
        if hash_prefix(src, args.archive.stat().st_size).hexdigest() != args.sha256:
            raise RuntimeError('Local archive SHA256 mismatch')
    command = remote_command(args.remote_path, args.sha256, args.archive.stat().st_size)
    # No terminal allocation: stdin carries raw bytes; SSH authentication uses the console.
    process = subprocess.Popen(['ssh.exe', *SSH_OPTIONS, '-T', args.target, command],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        send(process, args.archive, args.sha256, lambda msg: print(msg, flush=True))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('SSH_UPLOAD_STOPPED: ' + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)
