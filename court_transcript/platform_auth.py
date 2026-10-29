"""Opt-in request-bound platform identity; campus cookies remain a separate namespace."""
import asyncio
import hashlib
import hmac
import sqlite3

from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .platform_identity import IdentityError, checked_file, safe_target, verify_envelope

MAX_PART = 4 * 1024 * 1024


class PlatformIdentity:
    def __init__(self, app, public_key, access, tool_id='court-transcript', openssl='/usr/bin/openssl'):
        self.app, self.key, self.access, self.tool_id, self.openssl = app, public_key, access, tool_id, openssl
        checked_file(public_key)
        self.slots = asyncio.Semaphore(4)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        scope.setdefault('state', {})['platform_user'] = None
        tokens = [v for k, v in scope['headers'] if k.lower() == b'x-platform-tool-identity']
        if not tokens:
            return await self.app(scope, receive, send)
        async def deny(code, status=401):
            await JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store',
                               'X-Platform-Tool-Bridge': '1'})(scope, receive, send)
        if len(tokens) != 1 or self.slots.locked():
            return await deny('平台身份无效或请求繁忙', 401 if len(tokens) != 1 else 429)
        async with self.slots:
            try:
                if len(tokens[0]) > 6000:
                    raise IdentityError('invalid_identity')
                target = scope.get('raw_path', scope['path'].encode('ascii')).decode('ascii')
                if scope.get('query_string'):
                    target += '?' + scope['query_string'].decode('ascii')
                safe_target(target)
                claims = await run_in_threadpool(verify_envelope, self.key, tokens[0].decode('ascii'),
                    'tool:' + self.tool_id, scope['method'], target, openssl=self.openssl)
                accepted = await run_in_threadpool(self.access.consume_platform_nonce, claims['nonce'], claims['exp'])
                if not accepted:
                    raise IdentityError('replayed_identity')
                body = bytearray()
                more = True
                while more:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    body.extend(message.get('body', b''))
                    if len(body) > MAX_PART:
                        return await deny('平台请求超过分块上限', 413)
                    more = message.get('more_body', False)
                body = bytes(body)
                if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), claims['body_sha256']):
                    raise IdentityError('body_digest_mismatch')
                scope['state']['platform_user'] = {'id': 'platform:' + claims['sub'],
                    'label': claims['username'], 'csrf': claims['csrf']}
            except (IdentityError, ValueError, OSError, sqlite3.Error, UnicodeError):
                return await deny('平台身份无效或已过期')
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            async def marked(message):
                if message['type'] == 'http.response.start':
                    message = dict(message)
                    message['headers'] = list(message.get('headers', [])) + [(b'x-platform-tool-bridge', b'1')]
                await send(message)
            # Release the body-verification slot before a long streamed media response.
        await self.app(scope, replay, marked)
