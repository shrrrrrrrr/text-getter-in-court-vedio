"""Short-lived, request-bound tool identities; no passwords or calling Keys.

Ed25519 uses the operator's fixed OpenSSL executable. This standalone module
also runs on Python 3.10 tool hosts without installing the gateway dependencies.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit


class IdentityError(ValueError):
    pass


def require(value):
    if not value:raise IdentityError('invalid_platform_tool_identity')


def checked_file(path,*,private=False,maximum=32768):
    path=Path(path)
    require(path.is_absolute() and path.resolve()==path)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as stream:
        entry=os.fstat(stream.fileno())
        require(stat.S_ISREG(entry.st_mode) and entry.st_nlink==1 and entry.st_size<=maximum)
        if os.name!='nt':
            require(entry.st_uid==os.geteuid() if private else entry.st_uid in (0,os.geteuid()))
            require(stat.S_IMODE(entry.st_mode)==0o600 if private else not entry.st_mode&0o022)
        data=stream.read(maximum+1)
    require(len(data)<=maximum)
    return data


def safe_target(value):
    require(isinstance(value,str) and value.startswith('/') and not value.startswith('//')
            and value.isascii() and len(value)<=4096 and not any(ord(c)<32 or ord(c)==127 for c in value))
    parsed=urlsplit(value);require(not parsed.scheme and not parsed.netloc and not parsed.fragment)
    path=parsed.path
    for _ in range(4):
        require('\\' not in path and not any(ord(c)<32 or ord(c)==127 for c in path)
                and '..' not in path.split('/') and not path.startswith('//'))
        decoded=unquote(path)
        if decoded==path:break
        path=decoded
    else:raise IdentityError('invalid_platform_tool_identity')
    return value


def _b64(data):return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _decode(value):
    require(isinstance(value,str) and re.fullmatch('[A-Za-z0-9_-]+',value) is not None)
    return base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)


def _crypto(key,message,*,signature=None,workdir=None,openssl='/usr/bin/openssl'):
    checked_file(key,private=signature is None)
    # Only fixed local files and arguments reach the executable. Never a shell,
    # URL, provider name or caller-supplied command. No stderr/Key is reported.
    root=Path(workdir or tempfile.gettempdir()).resolve()
    directory=Path(tempfile.mkdtemp(prefix='tool-identity-',dir=root)).resolve()
    require(directory.parent==root)
    files=[]
    try:
        for name,data in (('message',message),('signature',signature)):
            if data is None:continue
            path=directory/name;files.append(path)
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:stream.write(data)
        args=[str(openssl),'pkeyutl','-rawin','-inkey',str(key),'-in',str(directory/'message')]
        args+=['-sign'] if signature is None else ['-verify','-pubin','-sigfile',str(directory/'signature')]
        result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=7)
        require(result.returncode==0)
        if signature is None:require(len(result.stdout)==64)
        return result.stdout
    except (OSError,subprocess.SubprocessError):
        raise IdentityError('platform_tool_crypto_unavailable') from None
    finally:
        for path in files:
            if path.exists():path.unlink()
        directory.rmdir()


FIELDS={'v','aud','sub','username','role','csrf','method','target','body_sha256','iat','exp','nonce'}


def validate(claims,audience,method,target,body,now=None,*,body_digest=None):
    now=time.time() if now is None else now
    require(isinstance(claims,dict) and set(claims)==FIELDS and claims['v']==1)
    require(isinstance(claims['sub'],str) and re.fullmatch('usr_[a-f0-9]{32}',claims['sub']) is not None)
    require(isinstance(claims['username'],str) and re.fullmatch('[a-z0-9][a-z0-9._-]{2,39}',claims['username']) is not None)
    require(claims['role'] in ('admin','caller') and isinstance(claims['csrf'],str)
            and re.fullmatch('[A-Za-z0-9_-]{32,128}',claims['csrf']) is not None)
    require(type(claims['iat']) is int and type(claims['exp']) is int
            and 0<claims['exp']-claims['iat']<=15 and claims['iat']<=now+3 and now<claims['exp'])
    require(isinstance(claims['nonce'],str) and re.fullmatch('[a-f0-9]{32}',claims['nonce']) is not None)
    require(body_digest is None or isinstance(body_digest,str) and re.fullmatch('[a-f0-9]{64}',body_digest) is not None)
    require(claims['aud']==audience and claims['method']==method and method in ('GET','HEAD','POST','PATCH')
            and claims['target']==safe_target(target) and claims['body_sha256']==
            (body_digest if body_digest is not None else hashlib.sha256(body).hexdigest()))
    return claims


def issue(key,audience,principal,csrf,method,target,body=b'',*,now=None,workdir=None,openssl='/usr/bin/openssl'):
    now=int(time.time() if now is None else now)
    claims={'v':1,'aud':audience,'sub':principal['id'],'username':principal['username'],'role':principal['role'],
            'csrf':csrf,'method':method,'target':safe_target(target),'body_sha256':hashlib.sha256(body).hexdigest(),
            'iat':now,'exp':now+15,'nonce':secrets.token_hex(16)}
    validate(claims,audience,method,target,body,now)
    raw=json.dumps(claims,sort_keys=True,separators=(',',':')).encode()
    return 'v1.'+_b64(raw)+'.'+_b64(_crypto(key,raw,workdir=workdir,openssl=openssl))


def verify(key,token,audience,method,target,body=b'',*,now=None,workdir=None,openssl='/usr/bin/openssl'):
    try:
        require(isinstance(token,str) and len(token)<=6000)
        version,payload,signature=token.split('.')
        require(version=='v1')
        raw,sig=_decode(payload),_decode(signature);require(len(sig)==64 and len(raw)<=4096)
        claims=validate(json.loads(raw),audience,method,target,body,now)
        _crypto(key,raw,signature=sig,workdir=workdir,openssl=openssl)
        return claims
    except (ValueError,KeyError,TypeError,UnicodeError):
        raise IdentityError('invalid_platform_tool_identity') from None


def verify_envelope(key,token,audience,method,target,*,now=None,workdir=None,openssl='/usr/bin/openssl'):
    """Authenticate headers at arrival. Caller MUST compare the full received body digest.

    This permits a bounded body to arrive after expiry without accepting a request
    whose headers were already expired. No application action may precede the digest check.
    """
    try:
        require(isinstance(token,str) and len(token)<=6000)
        version,payload,signature=token.split('.')
        require(version=='v1')
        raw,sig=_decode(payload),_decode(signature);require(len(sig)==64 and len(raw)<=4096)
        claims=json.loads(raw)
        require(isinstance(claims,dict) and isinstance(claims.get('body_sha256'),str)
                and re.fullmatch('[a-f0-9]{64}',claims['body_sha256']) is not None)
        validate(claims,audience,method,target,b'',now,body_digest=claims['body_sha256'])
        _crypto(key,raw,signature=sig,workdir=workdir,openssl=openssl)
        return claims
    except (ValueError,KeyError,TypeError,UnicodeError):
        raise IdentityError('invalid_platform_tool_identity') from None
