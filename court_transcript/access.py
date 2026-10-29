"""Local administrator-issued random credentials and revocable server-side sessions."""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

COOKIE = 'court_session_v1'
COOKIE_PATH = '/api/'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Access:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,label TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS owners(task_id TEXT PRIMARY KEY,user_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS limits(bucket TEXT PRIMARY KEY,started REAL NOT NULL,count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS platform_nonces(nonce TEXT PRIMARY KEY,expires INTEGER NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', ('csrf_secret', secrets.token_hex(32)))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_user(self, label):
        if not label.strip() or len(label) > 80:
            raise ValueError('显示名称需为 1–80 字符')
        user_id, key = secrets.token_hex(16), 'court_' + secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO users VALUES(?,?,?,1)', (user_id, label, digest(key)))
        return {'id': user_id, 'label': label, 'credential': key}

    def seed_user(self, record):
        user_id, label, key = record['id'], record['label'], record['credential']
        if not re.fullmatch('[a-f0-9]{32}', user_id) or not re.fullmatch(r'court_[A-Za-z0-9_-]{43}', key) or not 1 <= len(label) <= 80:
            raise ValueError('Invalid deployment credential seed')
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if row:
                if row['key_hash'] != digest(key) or row['label'] != label or not row['enabled']:
                    raise ValueError('Existing user differs; not overwritten or re-enabled')
            else:
                db.execute('INSERT INTO users VALUES(?,?,?,1)', (user_id, label, digest(key)))

    def revoke_user(self, user_id):
        with self.connect() as db:
            db.execute('UPDATE users SET enabled=0 WHERE id=?', (user_id,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))

    def limit_login(self, address):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM limits WHERE started<?', (now - 120,))
            rows = []
            for bucket, maximum in [('ip:' + digest(address), 20), ('global', 200)]:
                row = db.execute('SELECT * FROM limits WHERE bucket=?', (bucket,)).fetchone()
                count = row['count'] if row and now - row['started'] < 60 else 0
                if count >= maximum:
                    return False
                rows.append((bucket, row['started'] if count else now, count + 1))
            db.executemany('INSERT OR REPLACE INTO limits VALUES(?,?,?)', rows)
        return True

    def login(self, credential, previous=None):
        with self.connect() as db:
            user = db.execute('SELECT id,label FROM users WHERE key_hash=? AND enabled=1', (digest(credential),)).fetchone()
            if not user:
                return None
            if previous:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(previous),))
            token = secrets.token_urlsafe(32)
            now = time.time()
            db.execute('DELETE FROM sessions WHERE created<? OR last_seen<?', (now - 7 * 86400, now - 12 * 3600))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token), user['id'], now, now))
            # Bound live sessions per account; credentials themselves stay valid until revoked.
            db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN (SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created DESC LIMIT 8)', (user['id'], user['id']))
            return token

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT u.id,u.label,s.created,s.last_seen FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.enabled=1', (digest(token),)).fetchone()
            now = time.time()
            if not row or now - row['created'] >= 7 * 86400 or now - row['last_seen'] >= 12 * 3600:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))
                return None
            db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (now, digest(token)))
            return {'id': row['id'], 'label': row['label'], 'csrf': self.csrf(db, token)}

    def csrf(self, db, token):
        secret = db.execute('SELECT value FROM metadata WHERE name=?', ('csrf_secret',)).fetchone()[0]
        return hmac.new(bytes.fromhex(secret), token.encode(), hashlib.sha256).hexdigest()

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))

    def claim(self, task_id, user_id):
        with self.connect() as db:
            db.execute('INSERT INTO owners VALUES(?,?)', (task_id, user_id))

    def consume_platform_nonce(self, nonce, expires):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM platform_nonces WHERE expires<=?', (time.time(),))
            if db.execute('SELECT COUNT(*) FROM platform_nonces').fetchone()[0] >= 8192:
                return False
            try:
                db.execute('INSERT INTO platform_nonces VALUES(?,?)', (nonce, expires))
            except sqlite3.IntegrityError:
                return False
        return True

    def owned(self, task_id, user_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM owners WHERE task_id=? AND user_id=?', (task_id, user_id)).fetchone() is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--add-user')
    parser.add_argument('--revoke-user')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    access = Access(args.database)
    if args.add_user:
        if not args.output or args.output.exists():
            raise SystemExit('Provide a new private --output file; credentials are never printed')
        record = access.add_user(args.add_user)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(record, target, ensure_ascii=False, indent=2)
        print('Credential saved to the requested private file')
    elif args.revoke_user:
        access.revoke_user(args.revoke_user)
        print('User and sessions revoked')


if __name__ == '__main__':
    main()
