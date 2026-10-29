"""Read-only cloud/campus integration survey. Stdlib only; never import deployed apps.

Run via an operator's existing sudo session. Output is private deployment evidence.
No keys, environment files, databases, journals, requests with credentials or generation.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import glob
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
from urllib.parse import urlsplit


LIMIT = 2 * 1024 * 1024


def read_file(path, limit=LIMIT):
    # Do not follow a final symlink or accidentally read a device/FIFO.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('regular_file_required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('file_too_large')
        return raw
    finally:
        os.close(fd)


def command(args):
    # stdout may contain configuration secrets: callers only emit allowlisted summaries.
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=12,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if len(result.stdout) > LIMIT or len(result.stderr) > LIMIT:
        raise ValueError('command_output_too_large')
    return result.returncode, result.stdout.decode('utf-8', 'replace')


def service(name):
    properties = 'ActiveState,SubState,WorkingDirectory,FragmentPath,ExecStart'
    code, output = command(['systemctl', 'show', name, '--property=' + properties])
    if code:
        raise ValueError('unit_unavailable')
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    return values, {k: values.get(k, '') for k in properties.split(',') if k != 'ExecStart'}


def flag_value(exec_start, name):
    match = re.search(r'(?:^|\s)' + re.escape(name) + r'(?:=|\s+)([^\s;{}]+)', exec_start)
    return match.group(1) if match else None


def approved_path(value, bases):
    path = PurePosixPath(value)
    return path.is_absolute() and '..' not in path.parts and any(path.is_relative_to(base) for base in bases)


def observe(report, key, action):
    try:
        value = action()
        report[key] = value
        return value
    except Exception as exc:
        report[key] = {'error_type': type(exc).__name__}
        report['errors'].append(key + ':' + type(exc).__name__)
        return None


def safe_url(value):
    # Never reflect a username, password, query, fragment or arbitrary upstream path.
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return {'valid': False}
        return {'scheme': parsed.scheme, 'host': parsed.hostname, 'port': parsed.port,
                'path_present': parsed.path not in ('', '/'),
                'credentials_present': bool(parsed.username or parsed.password),
                'query_present': bool(parsed.query)}
    except (ValueError, TypeError):
        return {'valid': False}


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) else 'invalid'


def registry(path):
    raw = json.loads(read_file(path, 65536))
    rows = raw.get('tools', [])
    if not isinstance(rows, list):
        raise ValueError('registry_shape')
    return [{'id': identifier(row.get('id')), 'url': safe_url(row.get('url', '')),
             'embed': row.get('embed') is True} for row in rows[:20] if isinstance(row, dict)]


def source_summary(root):
    result = {}
    for relative in ('gateway/release.py', 'gateway/tool_proxy.py', 'gateway/__main__.py'):
        try:
            raw = read_file(root / relative)
            tree = ast.parse(raw)
            item = {'sha256': hashlib.sha256(raw).hexdigest()}
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    if 'UI_VERSION' in names and re.fullmatch(r'\d+\.\d+\.\d+', str(node.value.value)):
                        item['version'] = node.value.value
                    if 'MAX_RESPONSE' in names and isinstance(node.value.value, int):
                        item['max_response'] = node.value.value
            if relative.endswith('tool_proxy.py'):
                item['streaming_request_api_present'] = b'request.stream()' in raw
                item['whole_body_bytearray_present'] = b'body=bytearray()' in raw
                item['legacy_256k_guard_present'] = b'262144' in raw
                item['simcourt_routes_present'] = b'/api/cases' in raw
            result[relative] = item
        except (OSError, ValueError, SyntaxError) as exc:
            result[relative] = {'error_type': type(exc).__name__}
    return result


def nginx_summary(text, site):
    result = []
    parts = re.split(r'^# configuration file ([^\r\n]+):\s*$', text, flags=re.M)
    for offset in range(1, len(parts), 2):
        path, body = parts[offset:offset + 2]
        clean = '\n'.join(line.split('#', 1)[0] for line in body.splitlines())
        names = re.findall(r'\bserver_name\s+([^;]+);', clean)
        # Only this site's files; report no certificate, password, arbitrary header or log contents.
        if not any(site in line.split() for line in names):
            continue
        item = {'path': path, 'sha256': hashlib.sha256(body.encode()).hexdigest()}
        for directive, pattern in {
            'listen': r'[0-9A-Fa-f:.\[\] ]+(?:ssl)?(?:\s+http2)?',
            'client_max_body_size': r'[0-9]+[kKmMgG]?',
            'proxy_read_timeout': r'[0-9]+[smh]?',
            'proxy_request_buffering': r'on|off',
        }.items():
            item[directive] = [value.strip() for value in re.findall(
                r'\b' + directive + r'\s+([^;]+);', clean) if re.fullmatch(pattern, value.strip())]
        item['locations'] = [value.strip() for value in re.findall(r'\blocation\s+([^\{]+)\{', clean)
                             if re.fullmatch(r'[A-Za-z0-9_/.~*^= +\\-]{1,180}', value.strip())][:40]
        item['proxy_targets'] = [safe_url(value.strip()) for value in re.findall(r'\bproxy_pass\s+([^;]+);', clean)][:10]
        item['basic_auth_directive_present'] = bool(re.search(r'\bauth_basic\s+', clean))
        item['contains_503_return'] = bool(re.search(r'\breturn\s+503\b', clean))
        result.append(item)
    return result


def nginx_config_tree(root=Path('/etc/nginx')):
    """Read included files without running nginx -t/-T, which may open log files."""
    pending, seen, pieces, total = [root / 'nginx.conf'], set(), [], 0
    root = root.resolve()
    while pending:
        path = pending.pop(0).resolve(strict=True)
        module_conf = (root == Path('/etc/nginx') and path.is_relative_to('/usr/share/nginx/modules-available')
                       and path.suffix == '.conf')
        if not path.is_relative_to(root) and not module_conf:
            raise ValueError('nginx_include_outside_config_directory')
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > 120:
            raise ValueError('nginx_include_limit')
        raw = read_file(path, 256 * 1024)
        total += len(raw)
        if total > LIMIT:
            raise ValueError('nginx_total_limit')
        body = raw.decode('utf-8')
        pieces.append('# configuration file ' + str(path) + ':\n' + body + '\n')
        # shlex strips comments and respects quoted literal include paths.
        lexer = shlex.shlex(body, posix=True, punctuation_chars=';{}')
        tokens = [piece for token in lexer for piece in (list(token) if re.fullmatch(r'[;{}]+', token) else [token])]
        for index, token in enumerate(tokens):
            if (token != 'include' or (index and tokens[index - 1] not in (';', '{', '}'))
                    or index + 2 >= len(tokens) or tokens[index + 2] != ';'):
                continue
            pattern = tokens[index + 1]
            if '$' in pattern:
                raise ValueError('dynamic_nginx_include')
            candidate = Path(pattern)
            if not candidate.is_absolute():
                candidate = root / candidate
            matches = sorted(glob.glob(str(candidate)))
            if not matches and not glob.has_magic(str(candidate)):
                raise ValueError('missing_nginx_include')
            if len(matches) > 120:
                raise ValueError('nginx_glob_limit')
            pending.extend(Path(value) for value in matches)
    return ''.join(pieces)


def listeners(text):
    result = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        endpoint = fields[3]
        try:
            host, port = endpoint.rsplit(':', 1)
            port = int(port)
        except ValueError:
            continue
        if port in (22, 80, 443, 8766, 18765) or 28080 <= port <= 28100:
            result.append({'host': host, 'port': port})
    return result


def probe(host, port, path, *, host_header=None):
    connection = http.client.HTTPConnection(host, port, timeout=4)
    try:
        connection.request('GET', path, headers={'Host': host_header or host, 'Connection': 'close'})
        response = connection.getresponse()
        raw = response.read(65537)
        result = {'status': response.status}
        if len(raw) <= 65536 and response.status == 200:
            try:
                data = json.loads(raw)
                # Never reflect bodies, identifiers, tokens, task lists or model names.
                if isinstance(data, dict):
                    for key in ('required', 'authenticated', 'campus_http', 'web_accounts'):
                        if isinstance(data.get(key), bool):
                            result[key] = data[key]
            except (ValueError, UnicodeError):
                pass
        return result
    except (OSError, http.client.HTTPException) as exc:
        return {'error_type': type(exc).__name__}
    finally:
        connection.close()


def cloud(args, report):
    values, report['service'] = service('token-controller.service')
    root = Path(values.get('WorkingDirectory', ''))
    if not approved_path(values.get('WorkingDirectory', ''), ('/opt/token-controller',)):
        raise ValueError('unexpected_cloud_application_directory')
    report['source'] = source_summary(root)
    start = values.get('ExecStart', '')
    report['service']['web_accounts_flag'] = '--web-accounts' in start
    report['service']['tool_proxy_flag'] = '--tool-proxy-config' in start
    state = flag_value(start, '--state')
    if state == '/var/lib/token-controller':
        observe(report, 'registered_tools', lambda: registry(Path(state) / 'project-tools.local.json'))
    else:
        report['state_path_recognized'] = False
    config = flag_value(start, '--tool-proxy-config')
    if config:
        config_path = Path(config)
        if not approved_path(config, ('/etc/token-controller-api', '/var/lib/token-controller', '/etc/token-controller', '/etc/token-controller-tools')):
            report['proxy_config'] = {'error_type': 'unexpected_config_path'}
            report['errors'].append('proxy_config:unexpected_config_path')
        else:
            def proxy_summary():
                data = json.loads(read_file(config_path, 8192))
                return {'tool_id': identifier(data.get('tool_id')),
                        'upstream': safe_url(data.get('upstream', ''))}
            observe(report, 'proxy_config', proxy_summary)
    def nginx():
        output = nginx_config_tree()
        rows = nginx_summary(output, args.site)
        if not rows:
            raise ValueError('expected_site_not_found')
        return {'configuration_read_ok': True, 'syntax_test_executed': False, 'site_files': rows}
    observe(report, 'nginx', nginx)
    report['anonymous_platform_health'] = probe('127.0.0.1', 18765, '/health', host_header=args.site)
    sshd = shutil.which('sshd') or '/usr/sbin/sshd'
    def policy():
        code, output = command([sshd, '-T', '-C', 'user=tc-model-tunnel,host=localhost,addr=127.0.0.1'])
        if code:
            raise ValueError('ssh_configuration_sample_failed')
        return {line.split()[0]: line.split()[1:] for line in output.splitlines()
            if line.split() and line.split()[0] in ('allowtcpforwarding', 'gatewayports', 'permitlisten',
                                                   'permitopen', 'maxsessions', 'clientaliveinterval', 'clientalivecountmax')}
    observe(report, 'tunnel_policy_local_sample', policy)
    report['tunnel_policy_limit'] = 'local source sample only; real-source Match rules and authorized_keys may further restrict forwarding'


def campus(args, report):
    values, report['service'] = service('court-transcript-web.service')
    start = values.get('ExecStart', '')
    host, port = flag_value(start, '--host'), flag_value(start, '--port')
    if not host or not ipaddress.ip_address(host).is_private or port != '8766':
        raise ValueError('unexpected_campus_listener')
    report['anonymous_session'] = probe(host, int(port), '/api/auth/session')
    report['anonymous_tasks'] = probe(host, int(port), '/api/tasks')
    observe(report, 'model_tunnel_service', lambda: service('model-platform-tunnel.service')[1])
    # Only arguments from the public process unit, never its EnvironmentFile/credentials.
    root = Path(values.get('WorkingDirectory', ''))
    if root.is_absolute():
        for relative in ('court_transcript/app.py', 'court_transcript/access.py'):
            try:
                report.setdefault('source_sha256', {})[relative] = hashlib.sha256(read_file(root / relative)).hexdigest()
            except OSError as exc:
                report.setdefault('source_sha256', {})[relative] = {'error_type': type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('cloud', 'campus'), required=True)
    parser.add_argument('--site', default='models.example.test')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', args.site):
        parser.error('invalid site hostname')
    report = {'schema': 1, 'role': args.role, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'read_only': True, 'credential_files_opened': False, 'credentials_sent': False,
              'raw_configuration_emitted': False, 'generation_requested': False,
              'services_or_configurations_changed': False, 'errors': []}
    errors = []
    try:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise ValueError('run_using_existing_sudo_session')
        expected = 'token-controller.service' if args.role == 'cloud' else 'court-transcript-web.service'
        before, _ = service(expected)
        if before.get('ActiveState') != 'active':
            raise ValueError('expected_active_service_missing_or_wrong_server')
        def sockets():
            code, output = command(['ss', '-H', '-ltn'])
            if code:
                raise ValueError('listener_survey_failed')
            return listeners(output)
        observe(report, 'listeners', sockets)
        (cloud if args.role == 'cloud' else campus)(args, report)
    except Exception as exc:
        # Exceptions may contain configuration secrets: do not print str(exc) or tracebacks.
        errors.append(type(exc).__name__)
    errors.extend(report['errors'])
    report['errors'] = errors
    report['status'] = 'inspection_complete' if not errors else 'inspection_needs_review'
    print(json.dumps(report, ensure_ascii=False, indent=2))
    marker = 'INSPECTED' if not errors else 'NEEDS_REVIEW'
    print('COURT_INTEGRATION_' + args.role.upper() + '_' + marker, flush=True)
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
