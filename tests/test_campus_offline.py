import hashlib
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import campus_offline as off
from tools import campus_prepare as prep
from tools.build_campus_offline import dependency_closure, receiver_command
from tools.receive_campus_bundle import extract_verified


def wheel(directory, name, version='1.0', requires=()):
    path=directory/(name.replace('-','_')+'-'+version+'-py3-none-any.whl')
    raw='Metadata-Version: 2.1\nName: '+name+'\nVersion: '+version+'\nRequires-Python: >=3.10\n'
    raw+=''.join('Requires-Dist: '+r+'\n' for r in requires)
    with zipfile.ZipFile(path,'w') as z:z.writestr(name+'-'+version+'.dist-info/METADATA',raw)
    return path


def test_linux_python310_markers_not_windows_host_markers(tmp_path):
    wheel(tmp_path,'root',requires=['exceptiongroup>=1.0.2; python_version < "3.11"','windows-only; sys_platform == "win32"'])
    selected,missing=dependency_closure(tmp_path,'3.10',['root==1.0'])
    assert missing==['exceptiongroup>=1.0.2']
    wheel(tmp_path,'exceptiongroup','1.2.2')
    selected,missing=dependency_closure(tmp_path,'3.10',['root==1.0'])
    assert not missing and len(selected)==2


def test_pinned_hub_excludes_new_major_cache(tmp_path):
    wheel(tmp_path,'huggingface-hub','1.30.0')
    wheel(tmp_path,'huggingface-hub','2.3.0',requires=['unexpected-extra'])
    selected,missing=dependency_closure(tmp_path,'3.10',['huggingface-hub==1.30.0'])
    assert not missing and len(selected)==1 and '1.30.0' in selected[0].name


def test_direct_dependency_urls_refused(tmp_path):
    wheel(tmp_path,'root',requires=['other @ https://example.invalid/code.whl'])
    with pytest.raises(RuntimeError,match='direct dependency URL'):
        dependency_closure(tmp_path,'3.10',['root'])


def test_install_command_cannot_use_index_or_fetch_dependencies(tmp_path):
    args=off.offline_install_args(tmp_path/'python',[tmp_path/'package.whl'])
    assert '--no-index' in args and '--no-deps' in args and '--disable-pip-version-check' in args
    assert '--index-url' not in args and '--upgrade' not in args


def test_service_has_no_network_and_exact_gpu_mask(tmp_path):
    command=off.service_command(tmp_path,tmp_path/'venvs/cp310-test/bin/python3',tmp_path/'reports/smoke.py','GPU-synthetic',1001,1001,'test-unit','synthetic-only')
    assert '--property=PrivateNetwork=yes' in command
    assert '--property=ProtectSystem=strict' in command
    assert '--setenv=CUDA_VISIBLE_DEVICES=GPU-synthetic' in command
    assert '--property=MemoryMax=12G' in command
    assert not any('docker' in arg or 'http://' in arg or 'https://' in arg for arg in command)
    code=off.make_smoke_script()
    compile(code,'<offline-smoke>','exec')
    assert 'COURT_SMOKE_MODEL_DIR' in code and '"/models"' not in code


def test_cleanup_only_matching_owned_description(monkeypatch):
    calls=[]
    def run(args,**kw):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='some unrelated service')
    monkeypatch.setattr(prep,'run',run)
    off.clean_unit('test-unit','expected-description')
    assert len(calls)==1 and calls[0][:2]==['systemctl','show']


def archive_with(tmp_path, extra_name=None, symlink=False):
    archive=tmp_path/'test.tar'
    with tarfile.open(archive,'w') as tar:
        for name in ['manifest.json','tools/campus_prepare.py','tools/campus_offline.py']+([extra_name] if extra_name else []):
            info=tarfile.TarInfo(name)
            if symlink and name==extra_name:
                info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';tar.addfile(info)
            else:
                raw=b'synthetic';info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
    return archive,hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.mark.parametrize('bad_name',['../../outside','/absolute','tools/../outside'])
def test_archive_path_escape_rejected_before_extract(tmp_path,bad_name):
    archive,sha=archive_with(tmp_path,bad_name)
    dest=tmp_path/'dest';dest.mkdir()
    with pytest.raises(RuntimeError,match='unsafe archive entry'):
        extract_verified(archive,sha,dest)
    assert list(dest.iterdir())==[]


def test_archive_digest_and_symlink_rejected(tmp_path):
    archive,sha=archive_with(tmp_path,'link',True)
    dest=tmp_path/'dest';dest.mkdir()
    with pytest.raises(RuntimeError,match='SHA256 mismatch'):
        extract_verified(archive,'0'*64,dest)
    with pytest.raises(RuntimeError,match='unsafe archive entry'):
        extract_verified(archive,sha,dest)
    assert not list(dest.iterdir())


def test_receiver_quotes_and_validates_arguments():
    command=receiver_command('/safe/upload/bundle.tar','a'*64)
    assert command.startswith("python3 -c '") and 'base64' in command
    import shlex,base64,ast
    parts=shlex.split(command)
    assert parts[0:2]==['python3','-c'] and parts[4:] == ['/safe/upload/bundle.tar','a'*64]
    assert '"' not in command
    ast.parse(base64.b64decode(parts[3]).decode('utf-8'))
    with pytest.raises(RuntimeError,match='unsafe receiver'):
        receiver_command('/safe/$(run-code)','a'*64)


def synthetic_bundle(path,monkeypatch):
    monkeypatch.setattr(prep,'FILES',[('model.bin',3,hashlib.sha256(b'abc').hexdigest())])
    monkeypatch.setattr(prep,'FIXTURE_SIZE',3)
    monkeypatch.setattr(prep,'FIXTURE_SHA',hashlib.sha256(b'abc').hexdigest())
    names=['tools/campus_prepare.py','tools/campus_offline.py','models/model.bin','fixtures/jfk.flac',
           'wheels/pip-25.3-py3-none-any.whl','wheels/synthetic-1.0-py3-none-any.whl']+[f'extra-{n}' for n in range(4)]
    rows=[]
    for name in names:
        target=path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'abc')
        rows.append({'path':name,'size':3,'sha256':hashlib.sha256(b'abc').hexdigest()})
    manifest={'schema':1,'python_abi':'cp'+str(sys.version_info.major)+str(sys.version_info.minor),'files':rows}
    (path/'manifest.json').write_text(json.dumps(manifest),'utf-8')
    return manifest


def test_bundle_corruption_stops_before_installer(tmp_path,monkeypatch):
    synthetic_bundle(tmp_path,monkeypatch)
    assert off.verify_bundle(tmp_path)['schema']==1
    (tmp_path/'models/model.bin').write_bytes(b'bad')
    with pytest.raises(prep.Stop,match='integrity check failed'):
        off.verify_bundle(tmp_path)


def test_offline_orchestration_uses_no_server_downloads(tmp_path,monkeypatch):
    upload=tmp_path/'upload';upload.mkdir();synthetic_bundle(upload,monkeypatch)
    monkeypatch.setattr(prep.os,'chown',lambda *a:None,raising=False)
    monkeypatch.setattr(prep,'identity',lambda:(1001,1001,tmp_path))
    monkeypatch.setattr(prep,'available_ram_kib',lambda:128*1024**2)
    monkeypatch.setattr(off.shutil,'disk_usage',lambda p:SimpleNamespace(free=100*1024**3))
    monkeypatch.setattr(off.shutil,'which',lambda n:'/synthetic/'+n)
    monkeypatch.setattr(off.ctypes.util,'find_library',lambda n:'libgomp.so.1')
    monkeypatch.setattr(prep,'gpu_info',lambda i:{'uuid':'GPU-synthetic','used_mib':14,'free_mib':81169,'utilization':0})
    monkeypatch.setattr(prep.time,'sleep',lambda n:None)
    monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *a:None))
    monkeypatch.setattr(sys,'argv',['campus_offline.py','--bundle',str(upload)])
    def create(path):
        (path/'bin').mkdir(exist_ok=True);(path/'bin/python3').write_bytes(b'synthetic')
    monkeypatch.setattr(off.venv,'EnvBuilder',lambda **kw:SimpleNamespace(create=create))
    calls=[]
    def fake_subprocess(args,**kw):
        calls.append(args)
        assert '--no-index' in args or args[-1]=='check'
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(__import__('subprocess'),'run',fake_subprocess)
    def run(args,**kw):
        calls.append(args)
        if args[0]=='systemd-run':
            root=tmp_path/'court-transcript-runtime-v1'
            report={'status':'ASR_RUNTIME_TEST_PASSED','input_sha256':prep.FIXTURE_SHA,'audio_seconds':11,'load_seconds':1,'inference_seconds':1,'compute_type':'int8_float16'}
            (root/'reports/smoke-result.json').write_text(json.dumps(report),'utf-8')
        return SimpleNamespace(returncode=0,stdout='not-our-unit')
    monkeypatch.setattr(prep,'run',run)
    monkeypatch.setattr(prep,'download',lambda *a,**kw:pytest.fail('server network must never be used'))
    assert off.main()==0
    record=json.loads((tmp_path/'court-transcript-runtime-v1/status.json').read_text('utf-8'))
    assert record['server_network_downloads'] is False and record['status']=='passed'
    assert not any(arg in ['docker','curl','wget'] for command in calls for arg in command)
