"""TLS sidecar entry for the existing worker. No model loading or second Runner.

Run uvicorn with a verified certificate on loopback. The original application
verifies each identity and owns task execution; this process only relays bytes.
"""
import ipaddress
import os
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .platform_auth import MAX_PART
from .platform_identity import IdentityError, safe_target


class BodyTooLarge(ValueError):
    pass


def create_bridge(target=None, transport=None):
    target = target or os.getenv('COURT_BRIDGE_TARGET', 'http://127.0.0.1:8766')
    value = urlsplit(target)
    if (value.scheme != 'http' or not value.hostname or value.port != 8766
            or value.path not in ('', '/') or value.query or value.fragment or value.username or value.password
            or not ipaddress.ip_address(value.hostname).is_private):
        raise ValueError('Use one fixed private-address court worker on port 8766')
    target = target.rstrip('/')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PATCH'])
    async def relay(request: Request, path: str):
        tokens = request.headers.getlist('x-platform-tool-identity')
        if len(tokens) != 1 or not 1 <= len(tokens[0]) <= 6000:
            return JSONResponse({'detail': '平台身份必需'}, status_code=401)
        try:
            relative = request.scope['raw_path'].decode('ascii')
            if request.scope['query_string']:
                relative += '?' + request.scope['query_string'].decode('ascii')
            safe_target(relative)
        except (IdentityError, UnicodeError):
            return JSONResponse({'detail': '请求路径无效'}, status_code=400)
        async def incoming():
            used = 0
            async for chunk in request.stream():
                used += len(chunk)
                if used > MAX_PART:
                    raise BodyTooLarge()
                yield chunk
        headers = {name: request.headers[name] for name in ('x-platform-tool-identity', 'content-type', 'range')
                   if name in request.headers}
        client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                   timeout=httpx.Timeout(180, connect=5))
        response = None
        try:
            outgoing = client.build_request(request.method, target + relative, headers=headers, content=incoming())
            response = await client.send(outgoing, stream=True)
            if response.headers.get('x-platform-tool-bridge') != '1' or 300 <= response.status_code < 400:
                await response.aclose()
                await client.aclose()
                return JSONResponse({'detail': '已验证的庭录后端暂不可用'}, status_code=503)
        except (httpx.HTTPError, BodyTooLarge) as exc:
            if response is not None:
                await response.aclose()
            await client.aclose()
            return JSONResponse({'detail': '请求超过分块上限' if isinstance(exc, BodyTooLarge) else '庭录连接结果未知，请保留请求标识'},
                                status_code=413 if isinstance(exc, BodyTooLarge) else 502)
        selected = {k: v for k, v in response.headers.items() if k.lower() in
                    ('content-type', 'content-disposition', 'content-range', 'content-length', 'accept-ranges',
                     'x-platform-tool-bridge', 'cache-control')}
        async def stream():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()
        return StreamingResponse(stream(), status_code=response.status_code, headers=selected)

    return app


app = create_bridge()
