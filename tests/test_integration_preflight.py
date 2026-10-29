import argparse
import ast
import json
from pathlib import Path
import sys

import pytest

from tools import integration_preflight as survey
from tools.build_integration_preflight import commands


def test_private_urls_never_reflect_credentials_or_query():
    result = survey.safe_url('https://alice:secret-canary@127.0.0.1:28443/path?key=query-canary')
    text = json.dumps(result)
    assert 'secret-canary' not in text and 'query-canary' not in text and 'alice' not in text
    assert result['credentials_present'] and result['query_present']
    assert survey.safe_url('https://host:bad')['valid'] is False


def test_nginx_only_reports_site_limits_and_safe_routes():
    text = '''# configuration file /etc/nginx/sites-available/tool.conf:
server { server_name models.example.test; listen 443 ssl;
client_max_body_size 256k; proxy_read_timeout 660s;
proxy_set_header Authorization "Bearer secret-canary";
auth_basic_user_file /private/secret-canary;
location /tools/simcourt/ { proxy_pass https://user:secret-canary@127.0.0.1:28443/; }
}
# configuration file /etc/nginx/sites-available/other.conf:
server { server_name other.example.test; client_max_body_size 1g; }
'''
    rows = survey.nginx_summary(text, 'models.example.test')
    assert len(rows) == 1 and rows[0]['client_max_body_size'] == ['256k']
    assert rows[0]['locations'] == ['/tools/simcourt/']
    assert 'secret-canary' not in json.dumps(rows)
    assert 'Authorization' not in json.dumps(rows)


def test_include_tree_follows_only_loaded_files_and_respects_comments(tmp_path):
    (tmp_path / 'sites').mkdir()
    (tmp_path / 'nginx.conf').write_text('http { include "sites/*.conf"; } # include missing.conf;')
    (tmp_path / 'sites/active.conf').write_text('server { server_name models.example.test; }')
    (tmp_path / 'inactive.conf').write_text('private-inactive-canary')
    text = survey.nginx_config_tree(tmp_path)
    assert 'models.example.test' in text and 'private-inactive-canary' not in text


def test_include_outside_config_root_is_rejected(tmp_path):
    root = tmp_path / 'config'
    root.mkdir()
    (tmp_path / 'outside.conf').write_text('secret-canary')
    (root / 'nginx.conf').write_text('include ../outside.conf;')
    with pytest.raises(ValueError, match='outside'):
        survey.nginx_config_tree(root)


def test_regular_file_limit_and_symlink_guard(tmp_path):
    path = tmp_path / 'file'
    path.write_bytes(b'abcdef')
    with pytest.raises(ValueError, match='too_large'):
        survey.read_file(path, 3)
    if sys.platform != 'win32':
        link = tmp_path / 'link'
        link.symlink_to(path)
        with pytest.raises(OSError):
            survey.read_file(link)


def test_source_is_parsed_without_executing_it(tmp_path):
    (tmp_path / 'gateway').mkdir()
    (tmp_path / 'gateway/release.py').write_text("raise RuntimeError('never-execute')\nUI_VERSION = '0.11.0'\n")
    result = survey.source_summary(tmp_path)
    assert result['gateway/release.py']['version'] == '0.11.0'
    assert result['gateway/tool_proxy.py']['error_type'] == 'FileNotFoundError'


def test_listener_summary_never_returns_process_arguments():
    text = 'LISTEN 0 128 127.0.0.1:28080 0.0.0.0:* users:secret-canary\nLISTEN 0 128 0.0.0.0:5432 0.0.0.0:*\n'
    assert survey.listeners(text) == [{'host': '127.0.0.1', 'port': 28080}]


def test_errors_are_redacted_and_other_checks_can_continue():
    report = {'errors': []}
    def failure():
        raise ValueError('secret-canary')
    survey.observe(report, 'first', failure)
    survey.observe(report, 'second', lambda: {'ok': True})
    assert 'secret-canary' not in json.dumps(report) and report['second']['ok']


def test_campus_surveys_only_anonymous_gets_without_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(survey, 'service', lambda unit: (
        {'ExecStart': '{ /venv/python -m uvicorn app --host 10.20.30.40 --port 8766 ; }',
         'WorkingDirectory': ''}, {'ActiveState': 'active'}))
    monkeypatch.setattr(survey, 'probe', lambda host, port, path, **kw: calls.append((host, port, path)) or {'status': 401})
    report = {'errors': []}
    survey.campus(argparse.Namespace(), report)
    assert calls == [('10.20.30.40', 8766, '/api/auth/session'), ('10.20.30.40', 8766, '/api/tasks')]
    assert not report['errors']


def test_cloud_survey_command_allowlist_and_redacted_registry(monkeypatch, tmp_path):
    calls = []
    def command(args):
        calls.append(args)
        return 0, 'permitlisten 127.0.0.1:28080\ngatewayports no\nsetenv SECRET=secret-canary\n'
    monkeypatch.setattr(survey, 'command', command)
    monkeypatch.setattr(survey, 'service', lambda name: (
        {'WorkingDirectory': '/opt/token-controller/releases/example',
         'ExecStart': '--state /var/lib/token-controller --web-accounts --tool-proxy-config /etc/token-controller/proxy.json'},
        {'ActiveState': 'active'}))
    monkeypatch.setattr(survey, 'source_summary', lambda root: {'parsed_without_execution': True})
    reads = []
    def read(path, limit=0):
        reads.append(str(path))
        if str(path).endswith('proxy.json'):
            return json.dumps({'tool_id': 'simcourt', 'upstream': 'https://127.0.0.1:28443',
                               'private_key': '/private/never-open.key'}).encode()
        return b'{"tools":[{"id":"simcourt", "url":"https://models.example.test/tools/simcourt/"}]}'
    monkeypatch.setattr(survey, 'read_file', read)
    monkeypatch.setattr(survey, 'nginx_config_tree', lambda: '# configuration file /etc/nginx/tool.conf:\nserver { server_name models.example.test; }')
    monkeypatch.setattr(survey, 'probe', lambda *args, **kw: {'status': 200})
    report = {'errors': []}
    survey.cloud(argparse.Namespace(site='models.example.test'), report)
    assert not report['errors']
    assert len(calls) == 1 and '-T' in calls[0] and '-C' in calls[0]
    assert '/private/never-open.key' not in reads
    assert 'secret-canary' not in json.dumps(report)
    assert report['proxy_config']['tool_id'] == 'simcourt'


def test_main_stops_on_wrong_host_before_http_or_config(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['survey', '--role', 'cloud'])
    monkeypatch.setattr(survey.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(survey, 'service', lambda name: ({'ActiveState': 'inactive'}, {}))
    monkeypatch.setattr(survey, 'command', lambda args: pytest.fail('wrong-host command'))
    assert survey.main() == 1
    text = capsys.readouterr().out
    assert 'COURT_INTEGRATION_CLOUD_NEEDS_REVIEW' in text
    assert 'INSPECTED' not in text


def test_collector_is_python310_compatible_and_has_no_remote_shell_or_mutation_commands():
    source = Path(survey.__file__).read_text(encoding='utf-8')
    ast.parse(source, feature_version=(3, 10))
    for forbidden in ('systemctl restart', 'systemctl stop', 'docker ', 'useradd', 'usermod',
                      'nginx\', \'-T', 'write_text(', 'write_bytes(', 'unlink(', 'chmod('):
        assert forbidden not in source


def test_offline_wrapper_checks_payload_before_execution(capsys):
    source = b"print('fixture-read-only-executed')\n"
    block = commands('cloud', 'models.example.test', source)
    wrapper = block.split("<<'COURT_INTEGRATION_CHECK_PY'\n", 1)[1].split('\nCOURT_INTEGRATION_CHECK_PY', 1)[0]
    ast.parse(wrapper, feature_version=(3, 10))
    exec(wrapper, {})
    assert 'fixture-read-only-executed' in capsys.readouterr().out
    damaged = wrapper.replace(__import__('hashlib').sha256(source).hexdigest(), '0' * 64)
    with pytest.raises(SystemExit, match='SOURCE_CHECK_FAILED'):
        exec(damaged, {})
    assert 'fixture-read-only-executed' not in capsys.readouterr().out
    with pytest.raises(ValueError):
        commands('cloud', '$(unsafe)', source)
