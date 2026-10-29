"""Bounded signed upload parts with durable IDs and no implicit finalize replay."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .media import probe
from .platform_auth import MAX_PART
from .schema import Options, Task


class StartUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=250)
    size: int = Field(gt=0)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    options: Options = Field(default_factory=Options)


class Uploads:
    def __init__(self, access, store, settings, runner, suffixes, run_jobs):
        self.access, self.store, self.settings, self.runner = access, store, settings, runner
        self.suffixes, self.run_jobs = suffixes, run_jobs
        self.root = settings.data_dir.parent / 'platform-uploads'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        with access.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,request_id TEXT NOT NULL,
                filename TEXT NOT NULL,size INTEGER NOT NULL,options TEXT NOT NULL,
                state TEXT NOT NULL,created REAL NOT NULL,task_id TEXT,
                UNIQUE(user_id,request_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS upload_parts(
                upload_id TEXT NOT NULL,part INTEGER NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
                PRIMARY KEY(upload_id,part))''')

    def row(self, db, upload_id, user):
        if not re.fullmatch('[a-f0-9]{32}', upload_id):
            raise HTTPException(404, '上传不存在')
        row = db.execute('SELECT * FROM uploads WHERE id=? AND user_id=?', (upload_id, user)).fetchone()
        if not row:
            raise HTTPException(404, '上传不存在')
        return row

    def info(self, db, row):
        parts = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_parts WHERE upload_id=?', (row['id'],)).fetchone()
        present = [value[0] for value in db.execute('SELECT part FROM upload_parts WHERE upload_id=? ORDER BY part', (row['id'],))]
        return {'upload_id': row['id'], 'state': row['state'], 'part_size': MAX_PART,
                'received_parts': parts[0], 'parts_present': present, 'received_bytes': parts[1], 'size': row['size'], 'task_id': row['task_id']}

    def start(self, user, spec):
        filename = spec.filename.replace('\\', '/').rsplit('/', 1)[-1]
        if Path(filename).suffix.lower() not in self.suffixes:
            raise HTTPException(415, '不支持该视频或音频扩展名')
        if spec.size > self.settings.max_upload_bytes:
            raise HTTPException(413, '文件超过单任务大小限制')
        options = spec.options.model_dump_json()
        with self.lock, self.access.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM uploads WHERE user_id=? AND request_id=?', (user, spec.request_id)).fetchone()
            if old:
                if (old['filename'], old['size'], old['options']) != (filename, spec.size, options):
                    raise HTTPException(409, '上传请求标识已用于其他文件或选项')
                return self.info(db, old)
            active = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM uploads WHERE state IN ('receiving','finalizing')").fetchone()
            own = db.execute("SELECT COUNT(*) FROM uploads WHERE user_id=? AND state IN ('receiving','finalizing')", (user,)).fetchone()[0]
            if active[0] >= 8 or own >= 2:
                raise HTTPException(429, '上传队列已满，请先完成或取消已有上传')
            if shutil.disk_usage(self.root).free < 2 * (active[1] + spec.size) + 10 * 1024**3:
                raise HTTPException(507, '服务器磁盘不足，尚未开始上传')
            upload_id = secrets.token_hex(16)
            (self.root / upload_id).mkdir(mode=0o700)
            db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?, ?,NULL)',
                       (upload_id, user, spec.request_id, filename, spec.size, options, 'receiving', time.time()))
            return self.info(db, self.row(db, upload_id, user))

    def part(self, user, upload_id, index, body):
        digest = hashlib.sha256(body).hexdigest()
        with self.lock, self.access.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.row(db, upload_id, user)
            count = math.ceil(row['size'] / MAX_PART)
            if not 0 <= index < count:
                raise HTTPException(422, '分块编号无效')
            expected = min(MAX_PART, row['size'] - index * MAX_PART)
            if len(body) != expected:
                raise HTTPException(422, '分块长度不匹配')
            previous = db.execute('SELECT * FROM upload_parts WHERE upload_id=? AND part=?', (upload_id, index)).fetchone()
            if previous:
                if previous['sha256'] != digest:
                    raise HTTPException(409, '该分块已存在且内容不同')
                return self.info(db, row)
            if row['state'] != 'receiving':
                raise HTTPException(409, '该上传当前不可写入')
            if shutil.disk_usage(self.root).free < len(body) + 10 * 1024**3:
                raise HTTPException(507, '磁盘空间不足，保留已接收的分块')
            path = self.root / upload_id / str(index)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # A prior write may have reached disk before its DB transaction committed.
                if path.is_symlink() or path.stat().st_size != len(body) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise HTTPException(409, '分块写入结果需检查，未覆盖原文件')
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            db.execute('INSERT INTO upload_parts VALUES(?,?,?,?)', (upload_id, index, digest, len(body)))
            return self.info(db, row)

    def status(self, user, upload_id):
        with self.access.connect() as db:
            return self.info(db, self.row(db, upload_id, user))

    def pending(self, user):
        with self.access.connect() as db:
            return [self.info(db, row) for row in db.execute(
                "SELECT * FROM uploads WHERE user_id=? AND state IN ('receiving','finalizing','needs_review') ORDER BY created DESC LIMIT 20", (user,))]

    def abort(self, user, upload_id):
        with self.lock, self.access.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.row(db, upload_id, user)
            if row['state'] not in ('receiving', 'needs_review', 'aborted'):
                raise HTTPException(409, '仅接收中或合并失败的上传可以清理')
            db.execute("UPDATE uploads SET state='aborted' WHERE id=?", (upload_id,))
            # UUID directory checked against the owner's DB row; never remove task directories.
            if (self.root / upload_id).is_dir():
                shutil.rmtree(self.root / upload_id)
            return {'state': 'aborted'}

    def finish(self, user, upload_id):
        with self.lock:
            with self.access.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = self.row(db, upload_id, user)
                if row['state'] == 'completed':
                    return self.store.get(row['task_id'])
                if row['state'] != 'receiving':
                    raise HTTPException(409, '完成请求结果待检查，请保留上传标识，勿重新提交')
                parts = db.execute('SELECT * FROM upload_parts WHERE upload_id=? ORDER BY part', (upload_id,)).fetchall()
                if len(parts) != math.ceil(row['size'] / MAX_PART) or sum(p['size'] for p in parts) != row['size']:
                    raise HTTPException(409, '尚有分块未上传完成')
                if len(self.runner.events) >= 8:
                    raise HTTPException(429, '转写队列已满，上传分块已保留')
                if shutil.disk_usage(self.root).free < row['size'] + 10 * 1024**3:
                    raise HTTPException(507, '合并所需空间不足，上传分块已保留')
                task = Task(title=row['filename'][:200], filename=row['filename'], options=Options.model_validate_json(row['options']))
                db.execute("UPDATE uploads SET state='finalizing',task_id=? WHERE id=?", (task.id, upload_id))
            # Durable finalizing marker precedes file creation/submission. Unknown outcomes never auto replay.
            directory = self.store.directory(task.id)
            directory.mkdir()
            try:
                digest = hashlib.sha256()
                with (directory / 'input.media').open('xb') as target:
                    for part in parts:
                        path = self.root / upload_id / str(part['part'])
                        if path.is_symlink() or path.stat().st_size != part['size']:
                            raise ValueError('分块文件类型异常')
                        with path.open('rb') as source:
                            data = source.read(MAX_PART + 1)
                        if len(data) != part['size'] or hashlib.sha256(data).hexdigest() != part['sha256']:
                            raise ValueError('分块校验失败')
                        target.write(data)
                        digest.update(data)
                task.media = probe(directory / 'input.media')
                if task.media['duration'] > self.settings.max_duration or task.options.audio_stream >= len(task.media['audio_streams']):
                    raise ValueError('时长超过限制或所选音轨不存在')
                task.media['size'] = row['size']
                task.manifest['input_sha256'] = digest.hexdigest()
                self.access.claim(task.id, user)
                self.store.save(task)
                with self.access.connect() as db:
                    db.execute("UPDATE uploads SET state='completed' WHERE id=?", (upload_id,))
                if self.run_jobs:
                    try:
                        self.runner.submit(task.id)
                    except Exception:
                        task.state, task.error = 'interrupted', '转写未能入队，原文件已保留，可在任务列表恢复'
                        self.store.save(task)
                shutil.rmtree(self.root / upload_id)
                return task
            except Exception as exc:
                with self.access.connect() as db:
                    db.execute("UPDATE uploads SET state='needs_review' WHERE id=? AND state!='completed'", (upload_id,))
                if not (directory / 'task.json').exists():
                    # No task was published or submitted; retain the original parts, not a second failed copy.
                    shutil.rmtree(directory)
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(422, str(exc) if type(exc) is ValueError else '合并结果需检查，原分块及上传标识已保留') from None


def mount_uploads(app, access, store, settings, runner, suffixes, run_jobs):
    uploads = Uploads(access, store, settings, runner, suffixes, run_jobs)
    app.state.uploads = uploads
    def user(request):
        actor = getattr(request.state, 'platform_user', None)
        if not actor:
            raise HTTPException(403, '分块入口仅用于已验证的平台身份')
        return actor['id']

    @app.post('/api/uploads')
    async def start(request: Request, body: StartUpload):
        return await run_in_threadpool(uploads.start, user(request), body)

    @app.get('/api/uploads')
    def pending(request: Request):
        return uploads.pending(user(request))

    @app.get('/api/uploads/{upload_id}')
    def status(request: Request, upload_id: str):
        return uploads.status(user(request), upload_id)

    @app.post('/api/uploads/{upload_id}/parts/{index}')
    async def part(request: Request, upload_id: str, index: int):
        return await run_in_threadpool(uploads.part, user(request), upload_id, index, await request.body())

    @app.post('/api/uploads/{upload_id}/finish')
    async def finish(request: Request, upload_id: str):
        return await run_in_threadpool(uploads.finish, user(request), upload_id)

    @app.post('/api/uploads/{upload_id}/abort')
    def abort(request: Request, upload_id: str):
        return uploads.abort(user(request), upload_id)
