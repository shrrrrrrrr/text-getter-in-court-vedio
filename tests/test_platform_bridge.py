import hashlib
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import wave
from types import SimpleNamespace
import httpx

from fastapi.testclient import TestClient
import pytest

from court_transcript.access import Access
from court_transcript.app import create_app
from court_transcript.config import Settings
from court_transcript.platform_identity import issue
from court_transcript.platform_auth import MAX_PART
from court_transcript.platform_auth import PlatformIdentity
from court_transcript.uploads import StartUpload
from court_transcript.bridge import create_bridge

OPENSSL = shutil.which('openssl')
PERSON = {'id': 'usr_' + '1' * 32, 'username': 'synthetic-member', 'role': 'caller'}
CSRF = 'C' * 43


@pytest.fixture
def bridge(tmp_path):
    if not OPENSSL or os.name == 'nt':
        pytest.skip('native Linux OpenSSL required')
    private, public = tmp_path / 'private.pem', tmp_path / 'public.pem'
    subprocess.run([OPENSSL, 'genpkey', '-algorithm', 'Ed25519', '-out', str(private)], check=True, capture_output=True)
    subprocess.run([OPENSSL, 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
    private.chmod(0o600)
    settings = Settings(data_dir=tmp_path / 'tasks', access_db=tmp_path / 'access.sqlite',
                        allow_http=True, platform_public_key=public)
    app = create_app(settings, run_jobs=False)
    def sign(method, path, body=b'', person=None, **kwargs):
        return {'X-Platform-Tool-Identity': issue(private, 'tool:court-transcript', person or PERSON,
                CSRF, method, path, body, openssl=OPENSSL, **kwargs)}
    with TestClient(app) as client:
        yield app, client, sign, settings


def request(client, sign, method, path, body=b'', person=None):
    headers = sign(method, path, body, person)
    headers['Content-Type'] = 'application/json' if body.startswith(b'{') else 'application/octet-stream'
    return client.request(method, path, content=body, headers=headers)


def test_platform_identity_is_distinct_from_campus_users(bridge):
    app, client, sign, settings = bridge
    assert client.get('/api/tasks').status_code == 401
    response = request(client, sign, 'GET', '/api/auth/session')
    assert response.status_code == 200, response.text
    assert response.json()['mode'] == 'platform' and response.json()['campus_http'] is False
    assert response.headers['x-platform-tool-bridge'] == '1'
    result = request(client, sign, 'POST', '/api/tasks/demo')
    assert result.status_code == 200 or result.status_code == 201, result.text
    task = result.json()
    other = dict(PERSON, id='usr_' + '2' * 32)
    assert request(client, sign, 'GET', '/api/tasks', person=other).json() == []
    assert request(client, sign, 'GET', '/api/tasks/' + task['id'], person=other).status_code == 404
    renamed = dict(PERSON, username='renamed-member')
    assert request(client, sign, 'GET', '/api/tasks/' + task['id'], person=renamed).status_code == 200
    access = Access(settings.access_db)
    assert access.owned(task['id'], 'platform:' + PERSON['id'])
    assert request(client, sign, 'POST', '/api/auth/login', b'{}').status_code == 403


@pytest.mark.parametrize('fault', ['body', 'path', 'audience', 'expired', 'signature'])
def test_bound_identity_fails_closed_without_cookie_fallback(bridge, fault):
    app, client, sign, settings = bridge
    body = b'{}'
    header = sign('POST', '/api/tasks/demo', body, now=int(time.time()) - 20 if fault == 'expired' else int(time.time()))
    if fault == 'signature':
        pieces = header['X-Platform-Tool-Identity'].split('.')
        pieces[2] = ('A' if pieces[2][0] != 'A' else 'B') + pieces[2][1:]
        header['X-Platform-Tool-Identity'] = '.'.join(pieces)
    if fault == 'audience':
        header['X-Platform-Tool-Identity'] = header['X-Platform-Tool-Identity'].replace('v1.', 'v2.', 1)
    response = client.post('/api/tasks/demo' if fault != 'path' else '/api/uploads',
                           content=b'{"changed":true}' if fault == 'body' else body, headers=header)
    assert response.status_code == 401
    assert not app.state.store.all()


def test_identity_replay_and_unsigned_forged_headers_are_denied(bridge):
    app, client, sign, settings = bridge
    header = sign('GET', '/api/tasks')
    assert client.get('/api/tasks', headers=header).status_code == 200
    assert client.get('/api/tasks', headers=header).status_code == 401
    assert client.get('/api/tasks', headers={'X-Platform-User': PERSON['id']}).status_code == 401
    assert request(client, sign, 'POST', '/api/uploads', b'a' * (MAX_PART + 1)).status_code == 413


def start_upload(client, sign, data, request_id='a' * 32):
    body = json.dumps({'filename': 'control.wav', 'size': len(data), 'request_id': request_id, 'options': {}}).encode()
    result = request(client, sign, 'POST', '/api/uploads', body)
    assert result.status_code == 200, result.text
    return result.json()


def test_real_media_multipart_upload_finish_and_no_duplicate_task(bridge):
    if not shutil.which('ffprobe'):
        pytest.skip('real FFmpeg probe required')
    app, client, sign, settings = bridge
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * (MAX_PART // 2 + 1000))
    data = stream.getvalue()
    info = start_upload(client, sign, data)
    upload_id = info['upload_id']
    for index, offset in enumerate(range(0, len(data), MAX_PART)):
        chunk = data[offset:offset + MAX_PART]
        path = '/api/uploads/' + upload_id + '/parts/' + str(index)
        result = request(client, sign, 'POST', path, chunk)
        assert result.status_code == 200, result.text
        assert request(client, sign, 'POST', path, chunk).status_code == 200
    result = request(client, sign, 'POST', '/api/uploads/' + upload_id + '/finish')
    assert result.status_code == 200, result.text
    task = result.json()
    assert task['manifest']['input_sha256'] == hashlib.sha256(data).hexdigest()
    assert app.state.store.directory(task['id']).joinpath('input.media').read_bytes() == data
    again = request(client, sign, 'POST', '/api/uploads/' + upload_id + '/finish')
    assert again.status_code == 200 and again.json()['id'] == task['id']
    assert len(app.state.store.all()) == 1
    assert not (app.state.uploads.root / upload_id).exists()
    stranger = dict(PERSON, id='usr_' + '2' * 32)
    assert request(client, sign, 'GET', '/api/uploads/' + upload_id, person=stranger).status_code == 404


def test_upload_idempotence_wrong_parts_cancellation_and_ownership(bridge):
    app, client, sign, settings = bridge
    data = b'abc'
    first = start_upload(client, sign, data)
    assert start_upload(client, sign, data)['upload_id'] == first['upload_id']
    path = '/api/uploads/' + first['upload_id']
    assert request(client, sign, 'POST', path + '/finish').status_code == 409
    assert request(client, sign, 'POST', path + '/parts/0', b'x').status_code == 422
    assert request(client, sign, 'POST', path + '/parts/0', data).status_code == 200
    assert request(client, sign, 'POST', path + '/parts/0', b'xyz').status_code == 409
    stranger = dict(PERSON, id='usr_' + '2' * 32)
    assert request(client, sign, 'GET', '/api/uploads', person=stranger).json() == []
    assert request(client, sign, 'POST', path + '/abort', person=stranger).status_code == 404
    assert request(client, sign, 'POST', path + '/abort').status_code == 200
    assert not (app.state.uploads.root / first['upload_id']).exists()
    assert not app.state.store.all()


def test_unknown_finalization_is_not_replayed_and_limits_keep_original_data(bridge):
    app, client, sign, settings = bridge
    first = start_upload(client, sign, b'abc')
    upload_id = first['upload_id']
    request(client, sign, 'POST', '/api/uploads/' + upload_id + '/parts/0', b'abc')
    with app.state.uploads.access.connect() as db:
        db.execute("UPDATE uploads SET state='finalizing' WHERE id=?", (upload_id,))
    assert request(client, sign, 'POST', '/api/uploads/' + upload_id + '/finish').status_code == 409
    assert (app.state.uploads.root / upload_id / '0').read_bytes() == b'abc'
    start_upload(client, sign, b'abc', 'b' * 32)
    spec = json.dumps({'filename': 'control.wav', 'size': 3, 'request_id': 'c' * 32}).encode()
    assert request(client, sign, 'POST', '/api/uploads', spec).status_code == 429


def test_invalid_media_preserves_parts_and_failed_finish_does_not_repeat(bridge):
    if not shutil.which('ffprobe'):
        pytest.skip('real FFmpeg probe required')
    app, client, sign, settings = bridge
    info = start_upload(client, sign, b'not a wav')
    prefix = '/api/uploads/' + info['upload_id']
    assert request(client, sign, 'POST', prefix + '/parts/0', b'not a wav').status_code == 200
    assert request(client, sign, 'POST', prefix + '/finish').status_code == 422
    assert request(client, sign, 'POST', prefix + '/finish').status_code == 409
    assert (app.state.uploads.root / info['upload_id'] / '0').read_bytes() == b'not a wav'
    assert not list(settings.data_dir.glob('*/input.media'))
    assert request(client, sign, 'POST', prefix + '/abort').status_code == 200
    assert not (app.state.uploads.root / info['upload_id']).exists()


def test_tls_sidecar_relays_signed_identity_to_existing_app_without_second_worker(bridge):
    app, client, sign, settings = bridge
    relay = create_bridge('http://127.0.0.1:8766', transport=httpx.ASGITransport(app=app))
    with TestClient(relay) as front:
        assert front.get('/api/tasks').status_code == 401
        assert front.get('/docs').status_code == 401
        result = request(front, sign, 'GET', '/api/auth/session')
        assert result.status_code == 200 and result.json()['mode'] == 'platform'
        result = request(front, sign, 'POST', '/api/tasks/demo')
        assert result.status_code == 201, result.text
        assert app.state.store.get(result.json()['id']).is_demo
        assert not hasattr(relay.state, 'runner')
    for bad in ('http://public.example.test:8766', 'https://127.0.0.1:8766',
                'http://127.0.0.1:8766/path', 'http://127.0.0.1:22'):
        with pytest.raises(ValueError):
            create_bridge(bad)


def test_signature_admission_precedes_slow_body_but_digest_still_required(bridge, monkeypatch):
    app, client, sign, settings = bridge
    from court_transcript import platform_identity
    initial = int(time.time())
    clock = [initial]
    monkeypatch.setattr(platform_identity, 'time', SimpleNamespace(time=lambda: clock[0]))
    body = b'a bounded part arriving over a slow link'
    token = sign('POST', '/api/uploads', body, now=initial)['X-Platform-Tool-Identity']
    invoked = []
    async def inner(scope, receive, send):
        invoked.append(scope['state']['platform_user']['id'])
        assert (await receive())['body'] == body
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
    boundary = PlatformIdentity(inner, settings.platform_public_key, app.state.uploads.access)
    scope = {'type': 'http', 'path': '/api/uploads', 'raw_path': b'/api/uploads', 'method': 'POST',
             'headers': [(b'x-platform-tool-identity', token.encode())], 'query_string': b''}
    sent = []
    async def receive():
        clock[0] = initial + 30
        return {'type': 'http.request', 'body': body, 'more_body': False}
    async def send(message):
        sent.append(message)
    asyncio.run(boundary(scope, receive, send))
    assert invoked == ['platform:' + PERSON['id']]
    assert sent[0]['status'] == 200
    # A fresh attempt with that same expired token fails before its body is read.
    async def unread():
        pytest.fail('expired identity body must not be admitted')
    sent.clear()
    asyncio.run(boundary(dict(scope, state={}), unread, send))
    assert sent[0]['status'] == 401 and len(invoked) == 1
