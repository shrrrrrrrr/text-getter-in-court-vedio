"""Build offline paste blocks; private site names stay in ignored output files."""
import argparse
import base64
import gzip
import hashlib
from pathlib import Path
import re
import textwrap


def commands(role, site, source):
    if role not in ('cloud', 'campus') or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', site):
        raise ValueError('invalid role or hostname')
    digest = hashlib.sha256(source).hexdigest()
    payload = '\n'.join(textwrap.wrap(base64.b64encode(gzip.compress(source, mtime=0)).decode(), 96))
    return f'''# Run on {'Alibaba Cloud' if role == 'cloud' else 'the campus GPU server'} in Bash.
# Read-only, no install/restart/generation. Output is private deployment evidence.
(
set -eu
printf '%s\\n' 'COURT_INTEGRATION_{role.upper()}_CHECK_STARTED'
sudo -v
sudo -n /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \\
  /usr/bin/timeout 100 /usr/bin/python3 -I -B -u - --role {role} --site {site} <<'COURT_INTEGRATION_CHECK_PY'
import base64, hashlib, zlib
payload = b"""{payload}"""
decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
source = decoder.decompress(base64.b64decode(b''.join(payload.split()), validate=True), {len(source) + 1})
if (len(source) != {len(source)} or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
        or hashlib.sha256(source).hexdigest() != '{digest}'):
    raise SystemExit('COURT_INTEGRATION_SOURCE_CHECK_FAILED')
print('COURT_INTEGRATION_SOURCE_SHA256_OK', flush=True)
exec(compile(source, '<court-integration-read-only>', 'exec'), {{'__name__': '__main__'}})
COURT_INTEGRATION_CHECK_PY
)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--output', type=Path, default=Path('.state/deploy/integration-checks'))
    args = parser.parse_args()
    source = (Path(__file__).parent / 'integration_preflight.py').read_bytes()
    # Validate before creating private output files.
    blocks = {role: commands(role, args.site, source) for role in ('cloud', 'campus')}
    args.output.mkdir(parents=True, exist_ok=True)
    for role, block in blocks.items():
        path = args.output / (role + '-integration-check.commands.sh')
        path.write_text(block, encoding='utf-8', newline='\n')
        print(path)


if __name__ == '__main__':
    main()
