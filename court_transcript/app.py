from __future__ import annotations

import hashlib
import json
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .config import ROOT, Settings
from .demo import demo
from .engines import capabilities
from .export import FLAG_NAMES, json_export, markdown
from .media import frame, preview, probe
from .roles import ROLES
from . import role_agent
from .runner import Runner
from .schema import Options, SegmentEdit, Speaker, SpeakerEdit, Task
from .store import Conflict, Store

MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".3gp", ".3g2", ".vob", ".rm", ".rmvb", ".ogv", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".amr", ".wma", ".aiff", ".aif", ".caf", ".ape"}


class BodyLimit:
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = self.maximum + 1
        if size > self.maximum:
            return await JSONResponse({"detail": "上传文件超过本地大小限制"}, status_code=413)(scope, receive, send)
        used = 0

        async def bounded():
            nonlocal used
            message = await receive()
            used += len(message.get("body", b""))
            if used > self.maximum:
                raise HTTPException(413, "上传文件超过本地大小限制")
            return message

        await self.app(scope, bounded, send)


def create_app(settings=None, run_jobs=True):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir)
    runner = Runner(store, settings)
    preview_locks = {}
    preview_guard = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        runner.recover()
        yield
        runner.shutdown()

    app = FastAPI(title="庭录 · 庭审逐字转写", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.runner = store, runner
    app.add_middleware(BodyLimit, maximum=settings.max_upload_bytes + 1024 * 1024)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_boundary(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "拒绝跨站写入请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse({"detail": "任务或文件不存在"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "capabilities": {**capabilities(settings), "role_agent": role_agent.configured()},
                "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "roles": ROLES,
                "formats": sorted(MEDIA_SUFFIXES), "flags": FLAG_NAMES, "max_upload_bytes": settings.max_upload_bytes,
                "scope": "local-single-user", "recognition_accuracy": "not_validated"}

    @app.get("/api/tasks")
    def tasks():
        return [{k: t.model_dump()[k] for k in ["id", "title", "state", "stage", "progress", "created_at", "is_demo", "revision"]} for t in store.all()]

    @app.post("/api/tasks/demo", status_code=201)
    def create_demo():
        task = demo()
        store.save(task)
        return task

    @app.post("/api/tasks", status_code=202)
    async def upload(file: UploadFile = File(...), options: str = Form("{}")):
        try:
            opts = Options.model_validate_json(options)
        except ValueError:
            raise HTTPException(422, "处理配置无效；云端任务须明确授权，重叠分离需启用本地说话人分离")
        filename = (file.filename or "video").replace("\\", "/").rsplit("/", 1)[-1]
        if Path(filename).suffix.lower() not in MEDIA_SUFFIXES:
            raise HTTPException(415, "不支持该文件扩展名，请使用视频或音频容器文件（不接受播放列表）")
        task = Task(title=filename[:200], filename=filename[:250], options=opts)
        directory = store.directory(task.id)
        directory.mkdir()
        size, digest = 0, hashlib.sha256()
        try:
            with (directory / "input.media").open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "上传文件超过本地大小限制")
                    target.write(chunk)
                    digest.update(chunk)
            task.media = await run_in_threadpool(probe, directory / "input.media")
            if task.media["duration"] > settings.max_duration:
                raise HTTPException(422, "视频超过本地单任务 8 小时时长限制")
            if opts.audio_stream >= len(task.media["audio_streams"]):
                raise HTTPException(422, "所选音轨不存在；音轨编号从 0 开始")
            task.manifest["input_sha256"] = digest.hexdigest()
            task.media["size"] = size
            store.save(task)
            if run_jobs:
                runner.submit(task.id)
            return task
        except Exception as exc:
            # Only this newly created UUID directory belongs to the failed upload.
            shutil.rmtree(directory)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(422, str(exc) if type(exc) in {ValueError, RuntimeError} else "无法读取媒体文件")
        finally:
            await file.close()

    @app.get("/api/tasks/{task_id}")
    def get(task_id: str):
        return store.get(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        task = store.get(task_id)
        if not runner.cancel(task_id):
            raise HTTPException(409, "任务不在运行队列中")
        return {"status": "cancellation_requested", "cloud_continues": task.options.engine == "tencent"}

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str):
        with store.lock:
            task = store.get(task_id)
            if task.state not in {"failed", "cancelled", "interrupted"} or task.is_demo:
                raise HTTPException(409, "仅失败、中断或取消的真实任务可恢复")
            task.state, task.error, task.progress, task.stage = "queued", None, 0, "等待恢复处理"
            store.save(task)
            try:
                runner.submit(task_id)
            except ValueError as exc:
                task.state = "interrupted"
                store.save(task)
                raise HTTPException(409, str(exc))
        return task

    @app.patch("/api/tasks/{task_id}/segments/{segment_id}")
    def edit_segment(task_id: str, segment_id: str, edit: SegmentEdit):
        def mutate(task):
            segment = next((s for s in task.segments if s.id == segment_id), None)
            if segment is None:
                raise HTTPException(404, "片段不存在")
            if edit.speaker_id not in {s.id for s in task.speakers}:
                raise HTTPException(422, "说话人不存在，请先新增人物")
            if edit.end < edit.start or edit.end > task.media.get("duration", float("inf")) + 0.1:
                raise HTTPException(422, "时间范围超出媒体时长或结束早于开始")
            segment.text, segment.speaker_id = edit.text, edit.speaker_id
            segment.start, segment.end, segment.reviewed = edit.start, edit.end, edit.reviewed
        return store.edit(task_id, edit.revision, f"edit_segment:{segment_id}", mutate)

    @app.patch("/api/tasks/{task_id}/speakers/{speaker_id}")
    def edit_speaker(task_id: str, speaker_id: str, edit: SpeakerEdit):
        def mutate(task):
            speaker = next((s for s in task.speakers if s.id == speaker_id), None)
            if speaker is None:
                raise HTTPException(404, "人物不存在")
            if not set(edit.evidence_segment_ids) <= {s.id for s in task.segments}:
                raise HTTPException(422, "证据片段不存在")
            if edit.role_status == "confirmed" and not (edit.evidence_segment_ids or edit.evidence_note.strip()):
                raise HTTPException(422, "确认角色时请填写视频/核听证据")
            for key, value in edit.model_dump(exclude={"revision"}).items():
                setattr(speaker, key, value)
        return store.edit(task_id, edit.revision, f"edit_speaker:{speaker_id}", mutate)

    @app.post("/api/tasks/{task_id}/speakers")
    def add_speaker(task_id: str, data: dict):
        if not isinstance(data.get("revision"), int):
            raise HTTPException(422, "缺少版本号")
        def mutate(task):
            task.speakers.append(Speaker(id=f"PERSON_{len(task.speakers) + 1:02}", name="新人物"))
        return store.edit(task_id, data["revision"], "add_speaker", mutate)

    @app.get("/api/tasks/{task_id}/export/{kind}")
    def export(task_id: str, kind: str):
        task = store.get(task_id)
        if task.state != "completed":
            raise HTTPException(409, "任务未完成")
        directory = store.directory(task_id)
        if kind == "markdown":
            return Response(markdown(task), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="court-transcript.md"'})
        if kind == "json":
            return Response(json_export(task), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="court-transcript.json"'})
        if kind == "audit":
            path = directory / "audit.jsonl"
            return Response(path.read_text("utf-8") if path.exists() else "", media_type="application/x-ndjson", headers={"Content-Disposition": 'attachment; filename="review-audit.jsonl"'})
        if kind == "original" and (directory / "original-result.json").exists():
            return FileResponse(directory / "original-result.json", media_type="application/json", filename="original-result.json")
        raw_exports = {"asr-raw": "asr-raw.json", "mixture": "mixture-alternatives.json", "diarization": "diarization.json", "cloud-raw": "tencent-raw.json", "role-raw": "role-agent-raw.json"}
        if kind in raw_exports and (directory / raw_exports[kind]).exists():
            return FileResponse(directory / raw_exports[kind], media_type="application/json", filename=raw_exports[kind])
        raise HTTPException(404, "导出格式不存在")

    @app.get("/api/tasks/{task_id}/media/{kind}")
    def media(task_id: str, kind: str):
        store.get(task_id)
        directory = store.directory(task_id)
        mapping = {"audio": ("audio.wav", "audio/wav"), "preview": ("preview.mp4", "video/mp4")}
        if kind not in mapping:
            raise HTTPException(404, "媒体不存在")
        filename, mime = mapping[kind]
        path = directory / filename
        if not path.is_file():
            raise HTTPException(404, "媒体尚未就绪；原视频可生成通用回放副本")
        return FileResponse(path, media_type=mime)

    @app.get("/api/tasks/{task_id}/frame")
    def get_frame(task_id: str, seconds: float = 0):
        task = store.get(task_id)
        if not task.media.get("has_video") or not 0 <= seconds <= task.media["duration"]:
            raise HTTPException(422, "当前任务无画面或时间无效")
        directory = store.directory(task_id)
        path = directory / f"frame-{round(seconds * 1000)}.jpg"
        with preview_guard:
            lock = preview_locks.setdefault(task_id, threading.Lock())
        with lock:
            if not path.exists():
                try:
                    frame(directory / "input.media", path, seconds)
                except RuntimeError:
                    raise HTTPException(422, "画面提取失败")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/tasks/{task_id}/preview", status_code=202)
    def make_preview(task_id: str):
        task = store.get(task_id)
        if not task.media.get("has_video"):
            raise HTTPException(422, "没有视频画面")
        directory = store.directory(task_id)
        if (directory / "preview.mp4").exists():
            return {"status": "ready"}
        with preview_guard:
            lock = preview_locks.setdefault(task_id, threading.Lock())
            if not lock.acquire(blocking=False):
                return {"status": "processing"}
        def render():
            try:
                preview(directory / "input.media", directory / "preview.partial.mp4", task.options.audio_stream)
                (directory / "preview.partial.mp4").replace(directory / "preview.mp4")
                (directory / "preview-error.txt").unlink(missing_ok=True)
            except Exception:
                (directory / "preview-error.txt").write_text("视频回放副本转换失败；仍可播放已提取的音轨", "utf-8")
            finally:
                lock.release()
        threading.Thread(target=render, daemon=True).start()
        return {"status": "processing"}

    @app.get("/api/tasks/{task_id}/preview-status")
    def preview_status(task_id: str):
        store.get(task_id)
        directory = store.directory(task_id)
        return {"status": "ready" if (directory / "preview.mp4").exists() else "failed" if (directory / "preview-error.txt").exists() else "pending"}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
