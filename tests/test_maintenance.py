from pathlib import Path

from fastapi.testclient import TestClient

from court_transcript.app import create_app
from court_transcript.config import Settings


def test_maintenance_blocks_new_mutations_but_preserves_reads_and_existing_tasks(tmp_path):
    flag = tmp_path / 'maintenance'
    app = create_app(Settings(data_dir=tmp_path / 'tasks', maintenance_file=flag), run_jobs=False)
    with TestClient(app) as client:
        task = client.post('/api/tasks/demo').json()
        flag.write_text('operator maintenance')
        assert client.get('/api/health').json()['maintenance'] is True
        assert client.get('/api/tasks/' + task['id']).status_code == 200
        for path in ('/api/tasks/demo', '/api/tasks/' + task['id'] + '/retry'):
            response = client.post(path)
            assert response.status_code == 503 and response.headers['retry-after'] == '30'
        assert app.state.store.get(task['id']).is_demo
        flag.unlink()
        assert client.get('/api/health').json()['maintenance'] is False
        assert client.post('/api/tasks/demo').status_code == 201
