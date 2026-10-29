import json
import time

import pytest
from fastapi.testclient import TestClient

from court_transcript.access import Access, COOKIE
from court_transcript.app import create_app
from court_transcript.config import Settings


def make_clients(tmp_path):
    db = tmp_path / 'access.sqlite'
    access = Access(db)
    a, b = access.add_user('A'), access.add_user('B')
    settings = Settings(data_dir=tmp_path / 'tasks', access_db=db, allow_http=True)
    app = create_app(settings, run_jobs=False)
    return access, a, b, app


def login(client, user):
    result = client.post('/api/auth/login', json={'credential': user['credential']}, headers={'Origin': 'http://testserver'})
    assert result.status_code == 200, result.text
    cookie = result.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=lax' in cookie and 'path=/api/' in cookie
    info = client.get('/api/auth/session').json()
    client.headers.update({'Origin': 'http://testserver', 'X-CSRF-Token': info['csrf']})
    return info


def test_sessions_csrf_rotation_revocation_and_private_keys(tmp_path):
    access, a, _, app = make_clients(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/tasks').status_code == 401
        assert client.post('/api/auth/login', json={'credential': a['credential']}).status_code == 403
        login(client, a)
        old = client.cookies.get(COOKIE)
        assert client.post('/api/tasks/demo', headers={'X-CSRF-Token': 'bad'}).status_code == 403
        assert client.post('/api/tasks/demo', headers=[(b'origin', b'http://testserver'), (b'x-csrf-token', b'\xff')]).status_code == 403
        assert client.post('/api/tasks/demo', headers={'Origin': 'http://testserver:9999'}).status_code == 403
        assert client.post('/api/tasks/demo', headers={'Origin': 'null'}).status_code == 403
        login(client, a)
        assert client.cookies.get(COOKIE) != old and access.session(old) is None
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/tasks').status_code == 401
        login(client, a)
        access.revoke_user(a['id'])
        assert client.get('/api/tasks').status_code == 401
        assert a['credential'].encode() not in access.path.read_bytes()


def test_every_task_endpoint_checks_owner_before_body_or_file_access(tmp_path):
    access, a, b, app = make_clients(tmp_path)
    with TestClient(app) as first, TestClient(app) as second:
        login(first, a)
        login(second, b)
        task = first.post('/api/tasks/demo').json()
        assert len(first.get('/api/tasks').json()) == 1
        assert second.get('/api/tasks').json() == []
        root = '/api/tasks/' + task['id']
        paths = [('GET', ''), ('POST', '/cancel'), ('POST', '/retry'), ('POST', '/speakers'),
                 ('PATCH', '/segments/' + task['segments'][0]['id']), ('PATCH', '/speakers/' + task['speakers'][0]['id']),
                 ('GET', '/media/audio'), ('GET', '/media/preview'), ('GET', '/frame?seconds=0'),
                 ('POST', '/preview'), ('GET', '/preview-status')]
        paths += [('GET', '/export/' + name) for name in ['markdown', 'json', 'audit', 'original', 'asr-raw', 'mixture', 'diarization', 'cloud-raw', 'role-raw']]
        for method, path in paths:
            assert second.request(method, root + path).status_code == 404, (method, path)
        assert first.get(root + '/export/markdown').status_code == 200
        assert first.get(root, headers={'Host': 'testserver/api/auth/session?x='}).status_code == 400
        assert first.get(root, headers={'Host': 'evil.test'}).status_code == 400


def test_expired_session_rate_limit_and_unowned_legacy_task(tmp_path):
    access, a, _, app = make_clients(tmp_path)
    from court_transcript.demo import demo
    legacy = demo()
    app.state.store.save(legacy)
    with TestClient(app) as client:
        login(client, a)
        assert client.get('/api/tasks/' + legacy.id).status_code == 404
        with access.connect() as db:
            db.execute('UPDATE sessions SET last_seen=?', (time.time() - 13 * 3600,))
        assert client.get('/api/tasks').status_code == 401
        for _ in range(19):
            assert client.post('/api/auth/login', json={'credential': 'wrong' * 8}).status_code == 401
        assert client.post('/api/auth/login', json={'credential': a['credential']}).status_code == 429
        assert client.post('/api/auth/login', content=b'x' * 2048).status_code in {413, 429}


def test_http_is_explicit_and_seed_cannot_restore_revoked_user(tmp_path):
    access, a, _, app = make_clients(tmp_path)
    access.seed_user(a)
    access.revoke_user(a['id'])
    with pytest.raises(ValueError, match='not overwritten'):
        access.seed_user(a)
    protected = create_app(Settings(data_dir=tmp_path / 'secure', access_db=access.path))
    with TestClient(protected) as client:
        assert client.get('/api/auth/session').status_code == 403


def test_shared_upload_refuses_low_disk_before_processing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    access, a, _, app = make_clients(tmp_path)
    with TestClient(app) as client:
        login(client, a)
        monkeypatch.setattr('court_transcript.app.shutil.disk_usage', lambda path: SimpleNamespace(free=0))
        assert client.post('/api/tasks', files={'file': ('control.wav', b'not processed')}).status_code == 507
        assert client.get('/api/tasks').json() == []
