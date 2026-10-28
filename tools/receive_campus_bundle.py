"""SSH-delivered receiver. Verify the trusted archive digest before safe extraction."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def extract_verified(archive, expected, destination):
    h=hashlib.sha256()
    with archive.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    if not re.fullmatch('[a-f0-9]{64}',expected) or h.hexdigest()!=expected:
        raise RuntimeError('archive SHA256 mismatch; nothing extracted')
    with tarfile.open(archive,'r:') as tar:
        members=tar.getmembers()
        if len(members)>220 or sum(m.size for m in members)>16*1024**3:
            raise RuntimeError('archive exceeds expected bounds')
        seen=set()
        for item in members:
            path=PurePosixPath(item.name)
            if not item.isfile() or path.is_absolute() or '..' in path.parts or item.name in seen or not re.fullmatch(r'[A-Za-z0-9_.+/-]+',item.name):
                raise RuntimeError('unsafe archive entry')
            if item.size>4*1024**3:raise RuntimeError('oversize archive entry')
            seen.add(item.name)
        if not {'manifest.json','tools/campus_prepare.py','tools/campus_offline.py'}<=seen:
            raise RuntimeError('missing installer files')
        for item in members:
            target=destination/item.name
            target.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(item) as src,target.open('xb') as out:shutil.copyfileobj(src,out,4*1024*1024)
            os.chmod(target,0o600)


if __name__=='__main__':
    supplied=Path(sys.argv[1])
    if supplied.is_symlink():raise SystemExit('symlink archive refused')
    archive=supplied.resolve()
    home=Path.home().resolve()
    if not archive.is_relative_to(home) or archive.is_symlink():raise SystemExit('invalid upload location')
    if shutil.disk_usage(archive.parent).free<48*1024**3:raise SystemExit('less than 48 GiB free')
    target=Path(tempfile.mkdtemp(prefix='payload.',dir=archive.parent))
    print('VERIFY_AND_EXTRACT_OFFLINE_BUNDLE',flush=True)
    extract_verified(archive,sys.argv[2],target)
    # Standard sudo/SSH terminal prompts only: passwords are never read into a script.
    result=subprocess.run(['sudo','python3',str(target/'tools/campus_offline.py'),'--bundle',str(target),'--gpu','3'])
    raise SystemExit(result.returncode)
