"""Exercise an actual local ASR job with caller-provided synthetic audio/video.

Usage: python tools/smoke_e2e.py .state/speech-smoke.mkv
Requires a running configured local server, never selects cloud or downloads weights.
"""
import argparse
import json
import time
from pathlib import Path

import httpx

parser=argparse.ArgumentParser()
parser.add_argument('fixture',type=Path)
parser.add_argument('--base',default='http://127.0.0.1:8766')
args=parser.parse_args()
output=Path(__file__).resolve().parent.parent/'.state/e2e'
output.mkdir(exist_ok=True)
with httpx.Client(base_url=args.base,timeout=120,trust_env=False) as client:
    with args.fixture.open('rb') as f:
        response=client.post('/api/tasks',files={'file':(args.fixture.name,f)},data={'options':json.dumps({'engine':'local','language':'en','diarize':False})})
    response.raise_for_status()
    task=response.json()
    for _ in range(180):
        task=client.get('/api/tasks/'+task['id']).json()
        if task['state'] not in {'queued','running'}:
            break
        time.sleep(1)
    assert task['state']=='completed',task.get('error') or task['state']
    assert task['segments'] and all(s['words'] for s in task['segments'])
    assert task['speakers'][0]['role_status']=='unknown'
    assert len(task['manifest']['audio_sha256'])==64
    assert task['manifest']['asr_identity'].get('files_sha256')
    for kind in ['markdown','json','original','asr-raw']:
        result=client.get(f"/api/tasks/{task['id']}/export/{kind}")
        result.raise_for_status()
        (output/('transcript.md' if kind=='markdown' else kind+'.json')).write_bytes(result.content)
    if task['media']['has_video']:
        result=client.get(f"/api/tasks/{task['id']}/frame?seconds=1")
        result.raise_for_status()
        (output/'frame.jpg').write_bytes(result.content)
        client.post(f"/api/tasks/{task['id']}/preview").raise_for_status()
        for _ in range(60):
            result=client.get(f"/api/tasks/{task['id']}/preview-status").json()
            if result['status']!='pending':
                break
            time.sleep(1)
        assert result['status']=='ready',result
        assert client.get(f"/api/tasks/{task['id']}/media/preview").status_code==200
    (output/'report.json').write_text(json.dumps({'task_id':task['id'],'state':task['state'],'segments':len(task['segments']),'text':[s['text'] for s in task['segments']],'elapsed_seconds':task['manifest']['elapsed_seconds'],'checks':['upload','normalize','ASR','word timestamps','model fingerprint','role unknown','Markdown','JSON','original','raw ASR','frame and preview if video']},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'state':task['state'],'segments':len(task['segments']),'elapsed_seconds':task['manifest']['elapsed_seconds']},indent=2))
