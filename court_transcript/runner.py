from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time
import os
import re
import signal
import subprocess
import sys

from . import __version__
from .config import Settings
from .engines import LocalEngine, package_versions, tencent_run
from .media import Cancelled, audio, check_cancel, command, sha256
from .roles import propose
from . import role_agent


class Runner:
    def __init__(self, store, settings: Settings):
        self.store = store
        self.settings = settings
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="court-worker")
        self.events: dict[str, Event] = {}
        self.guard = Lock()
        self.local = LocalEngine(settings)
        self.defer_completion = False

    def recover(self):
        for task in self.store.all():
            if task.state in {"running", "queued"}:
                task.state = "interrupted"
                task.stage = "服务重启，任务可手动恢复"
                self.store.save(task)

    def submit(self, task_id):
        with self.guard:
            if task_id in self.events:
                raise ValueError("任务已在队列中")
            if len(self.events) >= 8:
                raise ValueError("本地任务队列已满（最多 8 个），请等待现有任务完成")
            event = Event()
            self.events[task_id] = event
            self.pool.submit(self.run_process if self.settings.worker_subprocess else self.run, task_id, event)

    def run_process(self, task_id, event):
        directory = self.store.directory(task_id)
        cancel_file = directory / 'worker.cancel'
        process = None
        try:
            if event.is_set():
                raise Cancelled()
            if self.settings.device == 'cuda':
                uuid = self.settings.gpu_uuid
                if not re.fullmatch(r'GPU-[a-fA-F0-9-]{16,}', uuid):
                    raise RuntimeError('GPU UUID 未配置，未启动识别进程')
                result = subprocess.run(['nvidia-smi', '-i', uuid, '--query-gpu=memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15, check=True)
                used, free, util = map(int, result.stdout.strip().split(','))
                if used > 1024 or free < 12 * 1024 or util > 5:
                    raise RuntimeError('指定 GPU 当前繁忙，未切换其他显卡或停止其他模型；请稍后重试')
            cancel_file.unlink(missing_ok=True)
            original = directory / 'original-result.json'
            if original.exists():
                original.rename(directory / ('original-result.previous-' + str(time.time_ns()) + '.json'))
            env = dict(os.environ, COURT_DATA_DIR=str(self.settings.data_dir), COURT_ASR_MODEL=self.settings.model,
                       COURT_DEVICE=self.settings.device, COURT_COMPUTE_TYPE=self.settings.compute_type,
                       COURT_ALLOW_MODEL_DOWNLOAD='1' if self.settings.allow_download else '0', COURT_WORKER_SUBPROCESS='0',
                       OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4')
            if self.settings.gpu_uuid:
                env['CUDA_VISIBLE_DEVICES'] = self.settings.gpu_uuid
            with (directory / 'worker.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, '-m', 'court_transcript.worker', '--task-id', task_id],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                           start_new_session=os.name == 'posix')
                cancelled_at = None
                while process.poll() is None:
                    if event.is_set():
                        cancel_file.touch(exist_ok=True)
                        cancelled_at = cancelled_at or time.monotonic()
                        if time.monotonic() - cancelled_at >= 5:
                            if os.name == 'posix':
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                    time.sleep(.2)
            task = self.store.get(task_id)
            if event.is_set():
                task.state, task.stage = 'cancelled', '已停止识别并退出任务进程'
                self.store.save(task)
            elif process.returncode != 0:
                raise RuntimeError('识别进程异常退出；已保留进程日志，请检查服务器依赖或资源')
            elif task.state in {'running', 'queued'}:
                if not original.is_file():
                    raise RuntimeError('识别进程未生成完成检查点，请检查私有日志')
                task.state, task.stage, task.progress = 'completed', '转写完成，等待核听', 100
                self.store.save(task)
        except Exception as exc:
            task = self.store.get(task_id)
            task.state = 'cancelled' if event.is_set() else 'failed'
            task.stage = '任务进程已结束'
            task.error = str(exc) if type(exc) is RuntimeError else '任务进程未能完成，请检查服务器资源和私有日志'
            self.store.save(task)
        finally:
            if process and process.poll() is None:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=20)
            with self.guard:
                self.events.pop(task_id, None)

    def cancel(self, task_id):
        with self.guard:
            event = self.events.get(task_id)
            if event:
                event.set()
                return True
            return False

    def run(self, task_id, event):
        task = self.store.get(task_id)
        directory = self.store.directory(task_id)
        started = time.monotonic()
        trace = []

        def update(stage, progress):
            task.stage = stage
            if not trace or trace[-1]["stage"] != stage:
                trace.append({"stage": stage, "elapsed_seconds": round(time.monotonic() - started, 3)})
            if progress is not None:
                task.progress = progress
            self.store.save(task)

        try:
            check_cancel(event)
            task.state = "running"
            update("提取原始音轨", 5)
            path = directory / "audio.wav"
            audio(directory / "input.media", path, task.options.audio_stream, event)
            task.manifest = {"pipeline_version": __version__, "input_sha256": task.manifest["input_sha256"],
                             "audio_sha256": sha256(path), "packages": package_versions(),
                             "ffmpeg": command(["ffmpeg", "-version"], timeout=10).decode(errors="replace").splitlines()[0],
                             "options": task.options.model_dump(), "device": self.settings.device,
                             "compute_type": self.settings.compute_type,
                             "role_resolver": "self-introduction-regex-v1", "prompt": None}
            self.store.save(task)
            if task.options.engine == "local":
                segments, warnings, manifest = self.local.run(path, directory, task.options, event, update)
            else:
                segments, warnings, manifest = tencent_run(path, directory, task.options, event, update)
            check_cancel(event)
            task.segments = segments
            task.warnings = warnings
            if not segments:
                task.warnings.append("没有识别到可转写语音；这不证明视频无发言，请检查音轨、音量及识别语言")
            update("分析身份线索并建立校对索引", 92)
            task.speakers = propose(segments)
            task.manifest.update(manifest)
            task.manifest["identity_method"] = "角色只产生候选，需人工确认；未执行视觉身份推断"
            if task.options.role_agent:
                update("本地角色 Agent 分析证据线索", 94)
                try:
                    task.manifest["role_agent"] = role_agent.analyze(task, directory, event)
                except Cancelled:
                    raise
                except Exception:
                    task.warnings.append("可选角色 Agent 失败，已保留逐字转写及规则候选；请检查本地 API、视觉支持与结构化输出")
            task.manifest["trace"] = trace
            task.manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
            task.state = "completed"
            task.error = None
            snapshot = task.model_copy(deep=True)
            snapshot.progress, snapshot.stage = 100, '转写完成，等待核听'
            (directory / "original-result.json").write_text(snapshot.model_dump_json(indent=2), "utf-8")
            if self.defer_completion:
                task.state = 'running'
                update('转写完成，正在释放识别进程', 99)
            else:
                update("转写完成，等待核听", 100)
        except Cancelled:
            task.state = "cancelled"
            task.stage = "已停止本地处理"
            if task.options.engine == "tencent":
                task.warnings.append("本地取消仅停止轮询，云端任务可能继续运行及计费；恢复时查询原任务")
            self.store.save(task)
        except Exception as exc:
            task.state = "failed"
            task.stage = "处理失败，可检查配置后恢复"
            # Provider/FFmpeg exceptions can embed credentials or absolute paths.
            task.error = str(exc) if type(exc) in {RuntimeError, ValueError} else f"处理组件异常（{type(exc).__name__}），请检查依赖与模型配置"
            self.store.save(task)
        finally:
            with self.guard:
                self.events.pop(task_id, None)

    def shutdown(self):
        with self.guard:
            for event in self.events.values():
                event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
