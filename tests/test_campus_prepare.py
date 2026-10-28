import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import campus_prepare as prep


def test_script_is_python310_and_embedded_smoke_compiles():
    ast.parse(Path(prep.__file__).read_text('utf-8'), feature_version=(3, 10))
    compile(prep.SMOKE, '<smoke>', 'exec')
    assert '@sha256:' in prep.BASE_IMAGE
    assert '--chmod=0644' in prep.DOCKERFILE
    assert '--no-cache-dir' in prep.DOCKERFILE
    assert 'av==16.1.0' in prep.DOCKERFILE


def test_model_pins_complete_no_branch_based_download():
    assert sum(row[1] for row in prep.FILES) < 4 * 1024**3
    for filename, size, sha in prep.FILES:
        assert size>0 and len(sha)==64
        urls=prep.sources(filename)
        assert all(url.startswith('https://') for url in urls)
        assert all('/master/' not in url and '/main/' not in url for url in urls)


def test_download_verified_then_cached_without_network(tmp_path):
    path=tmp_path/'model.bin'
    raw=b'fully synthetic weight bytes'
    sha=hashlib.sha256(raw).hexdigest()
    calls=[]
    def fake(args, **kwargs):
        calls.append(args)
        Path(args[args.index('--output')+1]).write_bytes(raw)
        return SimpleNamespace(returncode=0)
    prep.download(path,len(raw),sha,['https://example.invalid/fixed'],command=fake)
    prep.download(path,len(raw),sha,['https://example.invalid/fixed'],command=fake)
    assert len(calls)==1 and path.read_bytes()==raw
    assert '--continue-at' in calls[0] and '-q' in calls[0]
    assert '--insecure' not in calls[0] and '-k' not in calls[0]


def test_bad_final_file_never_overwritten(tmp_path):
    path=tmp_path/'model.bin';path.write_bytes(b'existing')
    with pytest.raises(prep.Stop,match='existing file differs'):
        prep.download(path,3,'a'*64,['https://example.invalid'],command=lambda *a,**kw:pytest.fail('no network allowed'))
    assert path.read_bytes()==b'existing'


def test_bad_download_not_promoted(tmp_path):
    path=tmp_path/'model.bin'
    def fake(args,**kwargs):
        Path(args[args.index('--output')+1]).write_bytes(b'bad')
        return SimpleNamespace(returncode=0)
    with pytest.raises(prep.Stop,match='SHA256/size mismatch'):
        prep.download(path,3,hashlib.sha256(b'new').hexdigest(),['https://example.invalid'],command=fake)
    assert not path.exists() and list(tmp_path.glob('*.part'))


def test_download_fallback_retains_source_specific_partials(tmp_path):
    path=tmp_path/'model.bin';raw=b'good'
    def fake(args,**kwargs):
        target=Path(args[args.index('--output')+1])
        target.write_bytes(b'pa' if args[-1].endswith('first') else raw)
        return SimpleNamespace(returncode=1 if args[-1].endswith('first') else 0)
    prep.download(path,len(raw),hashlib.sha256(raw).hexdigest(),['https://example.invalid/first','https://example.invalid/second'],command=fake)
    assert path.read_bytes()==raw
    assert (tmp_path/'model.bin.source-0.part').read_bytes()==b'pa'


def test_busy_gpu_refused_without_fallback():
    gpu=prep.parse_gpu('3, GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee, NVIDIA A100, 81920, 14, 81169, 0',3)
    prep.require_idle_gpu(gpu)
    gpu['used_mib']=2048
    with pytest.raises(prep.Stop,match='no other GPU selected'):
        prep.require_idle_gpu(gpu)


def test_gpu_uuid_not_shell_or_arbitrary_device():
    with pytest.raises(prep.Stop,match='unexpected GPU UUID'):
        prep.parse_gpu('3, all, NVIDIA A100, 81920, 14, 81169, 0',3)


def test_container_isolation_no_ports_or_docker_socket(tmp_path):
    args=prep.smoke_command(tmp_path,'GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',1001,1001,'synthetic')
    assert args[args.index('--gpus')+1]=='device=GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    assert args[args.index('--network')+1]=='none'
    assert '--read-only' in args and '--privileged' not in args
    assert '-p' not in args and '--publish' not in args
    assert not any('docker.sock' in a for a in args)
    assert args[args.index('--user')+1]=='1001:1001'


def test_cleanup_refuses_container_with_other_label(monkeypatch):
    calls=[]
    def fake(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='some-other-app\n')
    monkeypatch.setattr(prep,'run',fake)
    prep.clean_owned_container('synthetic')
    assert len(calls)==1 and 'inspect' in calls[0]


def test_docker_commands_ignore_remote_context(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST','tcp://example.invalid:2375')
    monkeypatch.setenv('DOCKER_CONTEXT','unrelated-context')
    observed=[]
    def fake(args,**kwargs):
        observed.append((args,kwargs))
        return SimpleNamespace(returncode=0,stdout='{}',stderr='')
    monkeypatch.setattr(prep.subprocess,'run',fake)
    prep.run(['docker','info'])
    args,kwargs=observed[0]
    assert args[:3]==['docker','--host','unix:///var/run/docker.sock']
    assert 'DOCKER_HOST' not in kwargs['env'] and 'DOCKER_CONTEXT' not in kwargs['env']


def test_unknown_existing_directory_preserved(tmp_path,monkeypatch):
    monkeypatch.setattr(prep.os,'chown',lambda *a:None,raising=False)
    path=tmp_path/'court-transcript-runtime-v1';path.mkdir()
    (path/'existing.txt').write_text('keep','utf-8')
    with pytest.raises(prep.Stop,match='existing unrecognized'):
        prep.prepare_directory(tmp_path,1001,1001)
    assert (path/'existing.txt').read_text()=='keep'


def test_full_prepare_orchestration_with_synthetic_dependencies(tmp_path,monkeypatch):
    monkeypatch.setattr(prep.os,'chown',lambda *a:None,raising=False)
    monkeypatch.setattr(prep,'identity',lambda:(1001,1001,tmp_path))
    monkeypatch.setattr(prep,'available_ram_kib',lambda:128*1024**2)
    monkeypatch.setattr(prep.shutil,'disk_usage',lambda p:SimpleNamespace(free=100*1024**3))
    monkeypatch.setattr(prep.shutil,'which',lambda p:'/synthetic/'+p)
    monkeypatch.setattr(prep,'gpu_info',lambda i:{'index':i,'uuid':'GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee','used_mib':14,'free_mib':81169,'utilization':0})
    monkeypatch.setattr(prep.time,'sleep',lambda n:None)
    monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *a:None))
    monkeypatch.setattr(sys,'argv',['campus_prepare.py','--gpu','3'])
    monkeypatch.setattr(prep,'download',lambda path,*a,**kw:Path(path).write_bytes(b'synthetic'))
    calls=[]
    def fake(args,**kwargs):
        calls.append(args)
        out=''
        code=0
        if args[:2]==['docker','info']:out='{"nvidia":{}}'
        elif args[:3]==['docker','image','inspect']:
            if args[-2]=='{{json .Config.Labels}}':code=1
            else:out='sha256:synthetic'
        elif args[:2]==['docker','inspect']:out=prep.PROFILE
        elif args[:2]==['docker','run']:
            result={'status':'ASR_RUNTIME_TEST_PASSED','input_sha256':prep.FIXTURE_SHA,'audio_seconds':11,'load_seconds':1,'inference_seconds':1,'compute_type':'int8_float16'}
            (tmp_path/'court-transcript-runtime-v1/reports/smoke-result.json').write_text(json.dumps(result),'utf-8')
        return SimpleNamespace(returncode=code,stdout=out)
    monkeypatch.setattr(prep,'run',fake)
    assert prep.main()==0
    record=json.loads((tmp_path/'court-transcript-runtime-v1/status.json').read_text('utf-8'))
    assert record['status']=='passed' and record['campus_site_deployed'] is False
    assert record['glm_called'] is False and record['other_services_changed'] is False
    assert not any('stop' in call or 'restart' in call for call in calls)
