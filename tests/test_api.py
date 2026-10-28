import io
import json
import wave

import pytest
from fastapi.testclient import TestClient

from court_transcript.app import create_app
from court_transcript.config import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), run_jobs=False)
    with TestClient(app) as c:
        yield c


def demo(client):
    res = client.post("/api/tasks/demo")
    assert res.status_code == 201
    return res.json()


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b'\0\0' * 16000)
    return buffer.getvalue()


def test_ui_and_local_boundary(client):
    assert client.get('/').status_code == 200
    assert client.get('/static/style.css').status_code == 200
    assert client.get('/static/fonts/ChillReunion-Round.woff2').status_code == 200
    assert client.get('/api/health').json()['recognition_accuracy'] == 'not_validated'
    assert client.post('/api/tasks/demo', headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/tasks', headers={'Host': 'evil.test'}).status_code == 400
    assert client.get('/api/tasks/not-a-valid-id').status_code == 404
    assert client.get('/.env').status_code == 404
    assert client.get('/.state/tasks').status_code == 404


def test_review_flow_conflicts_and_preserved_raw(client):
    task = demo(client)
    task_id, segment = task['id'], task['segments'][0]
    payload = {'revision':0, 'text':'现在开庭。', 'speaker_id':segment['speaker_id'], 'start':segment['start'], 'end':segment['end'], 'reviewed':True}
    res = client.patch(f'/api/tasks/{task_id}/segments/{segment["id"]}', json=payload)
    assert res.status_code == 200
    updated = res.json()
    assert updated['segments'][0]['original_text'] == segment['original_text']
    assert updated['segments'][0]['reviewed']
    assert client.patch(f'/api/tasks/{task_id}/segments/{segment["id"]}', json=payload).status_code == 409
    speaker = updated['speakers'][0]
    payload = {'revision':1, 'name':'审判长一', 'role':'审判长', 'role_status':'confirmed', 'evidence_note':'00:01 经回听及桌牌确认', 'evidence_segment_ids':[segment['id']]}
    assert client.patch(f'/api/tasks/{task_id}/speakers/{speaker["id"]}', json=payload).status_code == 200
    text = client.get(f'/api/tasks/{task_id}/export/markdown').text
    assert '人工确认' in text and '审判长一' in text and '00:00:00.800' in text
    assert len(client.get(f'/api/tasks/{task_id}/export/audit').text.splitlines()) == 2
    assert client.get(f'/api/tasks/{task_id}/media/audio').status_code == 404


def test_invalid_edits(client):
    t = demo(client)
    s, p = t['segments'][0], t['speakers'][0]
    base = f'/api/tasks/{t["id"]}'
    edit = {'revision':0,'text':'测试','speaker_id':'DOES_NOT_EXIST','start':0,'end':1,'reviewed':False}
    assert client.patch(f'{base}/segments/{s["id"]}',json=edit).status_code == 422
    edit.update(speaker_id=p['id'], end=999999)
    assert client.patch(f'{base}/segments/{s["id"]}',json=edit).status_code == 422
    role = {'revision':0,'name':'','role':'法官','role_status':'confirmed','evidence_segment_ids':[],'evidence_note':''}
    assert client.patch(f'{base}/speakers/{p["id"]}',json=role).status_code == 422
    role.update(evidence_segment_ids=['nonexistent'])
    assert client.patch(f'{base}/speakers/{p["id"]}',json=role).status_code == 422


def test_audio_upload_probe_and_validation(client):
    options = json.dumps({'diarize':False})
    res = client.post('/api/tasks', files={'file':('../safe.wav', wav(), 'audio/wav')}, data={'options':options})
    assert res.status_code == 202, res.text
    t = res.json()
    assert t['filename'] == 'safe.wav' and t['media']['duration'] == 1
    assert len(t['manifest']['input_sha256']) == 64
    assert client.get(f'/api/tasks/{t["id"]}/export/markdown').status_code == 409
    assert client.post('/api/tasks', files={'file':('bad.m3u8',b'#EXTM3U')}).status_code == 415
    assert client.post('/api/tasks', files={'file':('bad.mp4',b'not a video')}).status_code == 422
    assert client.post('/api/tasks', files={'file':('ok.wav',wav())}, data={'options':'{"engine":"tencent"}'}).status_code == 422
    assert client.post('/api/tasks', files={'file':('ok.wav',wav())}, data={'options':'{"audio_stream":31}'}).status_code == 422


def test_new_person_and_reassignment(client):
    t=demo(client)
    base=f'/api/tasks/{t["id"]}'
    t=client.post(base+'/speakers',json={'revision':0}).json()
    s=t['segments'][-1]
    res=client.patch(base+'/segments/'+s['id'],json={'revision':1,'text':'书记员记录完毕。','speaker_id':t['speakers'][-1]['id'],'start':s['start'],'end':s['end'],'reviewed':True})
    assert res.status_code == 200
    assert res.json()['segments'][-1]['speaker_id'].startswith('PERSON_')
