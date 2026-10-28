#!/usr/bin/env python3
"""One foreground, resumable campus ASR runtime preparation. Linux stdlib only.

Run the hash-verified script through sudo. No host Python installs, daemon changes,
published ports, GLM calls, existing-container stops, or automatic model switching.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread

PROFILE = "court-asr-prepare-v1"
HF_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
MS_REVISION = "792d4a7900e25db2618b6f0655fad2ec9b41df7c"
BASE_IMAGE = "python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
FILES = [
    ("config.json", 2394, "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9"),
    ("preprocessor_config.json", 340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
    ("tokenizer.json", 2480617, "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca"),
    ("vocabulary.json", 1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
    ("model.bin", 3087284237, "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
]
FIXTURE_URL = "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/9e472ae9de8bb374552f531f752654554b76b2d6/tests/data/jfk.flac"
FIXTURE_SIZE = 1152693
FIXTURE_SHA = "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"

SMOKE = r'''import hashlib,json,math,os,time
from pathlib import Path
import ctranslate2
from faster_whisper import WhisperModel

assert ctranslate2.get_cuda_device_count()==1, "Exactly one GPU must be visible"
types=ctranslate2.get_supported_compute_types("cuda",0)
assert "int8_float16" in types, "int8_float16 unavailable"
started=time.monotonic()
model=WhisperModel("/models",device="cuda",device_index=0,compute_type="int8_float16",local_files_only=True,cpu_threads=4,num_workers=1)
loaded=time.monotonic()
segments,info=model.transcribe("/fixtures/jfk.flac",language="en",beam_size=5,temperature=0,word_timestamps=True,vad_filter=True,condition_on_previous_text=False)
rows=[]
for s in segments:
    assert math.isfinite(s.start) and math.isfinite(s.end) and 0<=s.start<=s.end
    words=[{"start":w.start,"end":w.end,"text":w.word,"probability":w.probability} for w in (s.words or [])]
    assert words and all(math.isfinite(w["start"]) and math.isfinite(w["end"]) and 0<=w["start"]<=w["end"] for w in words)
    rows.append({"start":s.start,"end":s.end,"text":s.text,"words":words})
text=" ".join(s["text"] for s in rows)
assert len(text.split())>=10 and "country" in text.lower(), "Public smoke speech not recognized"
report={"status":"ASR_RUNTIME_TEST_PASSED","model":"Whisper large-v3","compute_type":"int8_float16","visible_cuda_devices":1,"audio_seconds":info.duration,"load_seconds":round(loaded-started,3),"inference_seconds":round(time.monotonic()-loaded,3),"segments":rows,"ctranslate2":ctranslate2.__version__,"input_sha256":hashlib.sha256(Path("/fixtures/jfk.flac").read_bytes()).hexdigest(),"limitations":["English public control sample only","Not a Chinese court quality evaluation","No diarization, overlap separation or GLM connection tested"]}
Path("/output/smoke-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print("ASR_RUNTIME_TEST_PASSED",flush=True)
'''

DOCKERFILE = """FROM {base}
RUN ["apt-get", "update"]
RUN ["apt-get", "install", "-y", "--no-install-recommends", "libgomp1", "ca-certificates"]
RUN ["python", "-m", "pip", "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple", "faster-whisper==1.2.1", "av==16.1.0", "ctranslate2==4.8.4", "numpy>=2,<3", "nvidia-cublas-cu12==12.8.4.1", "nvidia-cudnn-cu12==9.13.1.26"]
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
ENV HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache CUDA_CACHE_PATH=/tmp/cuda-cache
COPY --chmod=0644 smoke.py /opt/court-smoke.py
LABEL org.otherworld.court-prep={profile}
CMD ["python", "/opt/court-smoke.py"]
""".format(base=BASE_IMAGE, profile=PROFILE)
BUILD_HASH = hashlib.sha256((DOCKERFILE + SMOKE).encode()).hexdigest()
IMAGE = "court-transcript-asr:prep-" + BUILD_HASH[:12]


class Stop(RuntimeError):
    pass


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(args, *, timeout=120, check=True, capture=True):
    command_env = os.environ.copy()
    if args[0] == "docker":
        args = ["docker", "--host", "unix:///var/run/docker.sock"] + args[1:]
        command_env.pop("DOCKER_HOST", None)
        command_env.pop("DOCKER_CONTEXT", None)
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, timeout=timeout, text=True, env=command_env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Stop("command unavailable or timed out: " + args[0]) from exc
    if check and result.returncode:
        # Don't echo raw daemon, signed URL, or proxy details to the returned summary.
        raise Stop("command failed: " + args[0] + " (exit " + str(result.returncode) + ")")
    return result


def sources(filename):
    return [
        "https://modelscope.cn/models/pengzhendong/faster-whisper-large-v3/resolve/" + MS_REVISION + "/" + filename,
        "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/" + HF_REVISION + "/" + filename,
    ]


def download(path, size, sha, urls, command=run):
    """Resume by source; verify all bytes. Existing invalid final files are never overwritten."""
    path = Path(path)
    if path.is_symlink():
        raise Stop("symlink target refused")
    if path.exists():
        if path.stat().st_size == size and digest(path) == sha:
            print("VERIFIED_CACHED " + path.name, flush=True)
            return
        raise Stop("existing file differs from fixed manifest: " + path.name)
    for index, url in enumerate(urls):
        part = path.with_name(path.name + ".source-" + str(index) + ".part")
        if part.is_symlink() or (part.exists() and part.stat().st_size > size):
            raise Stop("invalid partial file: " + path.name)
        print("DOWNLOAD " + path.name + " source=" + str(index + 1), flush=True)
        args = ["curl", "-q", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
                "--connect-timeout", "15", "--max-time", "3600", "--retry", "2", "--retry-delay", "3",
                "--continue-at", "-", "--output", str(part), url]
        if not part.exists() or part.stat().st_size != size:
            result = command(args, timeout=10900, check=False, capture=False)
            if result.returncode:
                continue
        if part.stat().st_size != size or digest(part) != sha:
            raise Stop("download SHA256/size mismatch, partial retained: " + path.name)
        part.replace(path)
        print("VERIFIED " + path.name, flush=True)
        return
    raise Stop("all normal-TLS download sources failed: " + path.name + "; cached partials retained")


def parse_gpu(csv_text, index):
    rows = list(csv.reader(csv_text.strip().splitlines()))
    for values in rows:
        values = [v.strip() for v in values]
        if len(values) == 7 and values[0] == str(index):
            record = dict(zip(["index", "uuid", "name", "total_mib", "used_mib", "free_mib", "utilization"], values))
            for key in ["index", "total_mib", "used_mib", "free_mib", "utilization"]:
                record[key] = int(record[key])
            if not re.fullmatch(r"GPU-[a-fA-F0-9-]{16,}", record["uuid"]):
                raise Stop("unexpected GPU UUID")
            return record
    raise Stop("selected GPU not found")


def gpu_info(index):
    result = run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"])
    return parse_gpu(result.stdout, index)


def require_idle_gpu(record):
    if record["used_mib"] > 1024 or record["free_mib"] < 12 * 1024 or record["utilization"] > 5:
        raise Stop("selected GPU is busy; no other GPU selected and no model stopped")


def identity():
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise Stop("Linux x86_64 required")
    if os.geteuid() != 0:
        raise Stop("run the verified script using sudo python3")
    import pwd
    raw_uid = os.environ.get("SUDO_UID", "")
    if not raw_uid.isdigit() or int(raw_uid) == 0:
        raise Stop("start from your ordinary SSH account using sudo; do not run from a root login")
    owner = pwd.getpwuid(int(raw_uid))
    return owner.pw_uid, owner.pw_gid, Path(owner.pw_dir).resolve()


def prepare_directory(home, uid, gid):
    root = home / "court-transcript-runtime-v1"
    if root.is_symlink() or (root.exists() and root.resolve().parent != home):
        raise Stop("unsafe runtime root")
    marker = root / "owner.json"
    if root.exists():
        if not marker.is_file() or marker.is_symlink():
            raise Stop("existing unrecognized runtime directory; nothing overwritten")
        saved = json.loads(marker.read_text("utf-8"))
        if saved != {"profile": PROFILE, "uid": uid}:
            raise Stop("runtime owner/profile mismatch")
    else:
        root.mkdir(mode=0o700)
        marker.write_text(json.dumps({"profile": PROFILE, "uid": uid}), "utf-8")
    os.chown(root, uid, gid)
    os.chown(marker, uid, gid)
    for name in ["models", "fixtures", "build", "reports"]:
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise Stop("unsafe runtime subdirectory")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, uid, gid)
    return root


def smoke_command(root, uuid, uid, gid, name):
    if any("," in str(root / p) for p in ["models", "fixtures", "reports"]):
        raise Stop("commas in runtime path unsupported")
    return ["docker", "run", "--name", name, "--label", "org.otherworld.court-prep=" + PROFILE,
            "--gpus", "device=" + uuid, "--user", str(uid) + ":" + str(gid), "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--cpus", "8", "--memory", "12g", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--mount", "type=bind,src=" + str(root / "models") + ",dst=/models,readonly",
            "--mount", "type=bind,src=" + str(root / "fixtures") + ",dst=/fixtures,readonly",
            "--mount", "type=bind,src=" + str(root / "reports") + ",dst=/output", IMAGE]


def clean_owned_container(name):
    result = run(["docker", "inspect", "--format", '{{index .Config.Labels "org.otherworld.court-prep"}}', name], check=False)
    if result.returncode == 0 and result.stdout.strip() == PROFILE:
        run(["docker", "rm", "--force", name])


def write_owned(path, content, uid, gid):
    if path.is_symlink():
        raise Stop("symlink output refused")
    path.write_text(content, "utf-8")
    os.chown(path, uid, gid)
    os.chmod(path, 0o600)


def available_ram_kib():
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
    return int(memory["MemAvailable"].split()[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", type=int, default=3)
    args = parser.parse_args()
    uid, gid, home = identity()
    root = prepare_directory(home, uid, gid)
    import fcntl
    lock_path = root / "prepare.lock"
    if lock_path.is_symlink():
        raise Stop("symlink lock refused")
    lock = lock_path.open("a")
    os.chown(lock_path, uid, gid)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Stop("another preparation is active; do not start a second copy")
    status = {"profile": PROFILE, "status": "running", "stage": "preflight", "started_at": time.time(),
              "model": "Whisper large-v3 engineering baseline", "hf_revision": HF_REVISION,
              "model_source": "ModelScope mirror with SHA256 matched to fixed SYSTRAN HF revision",
              "campus_site_deployed": False, "diarization_configured": False,
              "glm_called": False, "other_services_changed": False}
    def stage(value):
        status["stage"] = value
        write_owned(root / "status.json", json.dumps(status, ensure_ascii=False, indent=2), uid, gid)
        print("STAGE " + value, flush=True)
    name = "court-asr-smoke-" + str(os.getpid()) + "-" + str(int(time.time()))
    try:
        stage("preflight")
        if shutil.disk_usage(root).free < 48 * 1024**3:
            raise Stop("less than 48 GiB free; keep existing GLM/weights intact")
        if available_ram_kib() < 20 * 1024**2:
            raise Stop("less than 20 GiB available host RAM")
        for binary in ["docker", "curl", "nvidia-smi"]:
            if not shutil.which(binary):
                raise Stop("required command missing: " + binary)
        runtime = json.loads(run(["docker", "info", "--format", "{{json .Runtimes}}"]).stdout)
        if "nvidia" not in runtime:
            raise Stop("NVIDIA runtime not registered; no daemon changes attempted")
        initial = gpu_info(args.gpu)
        require_idle_gpu(initial)
        status["gpu_before"] = initial
        stage("download_fixed_model_and_public_fixture")
        for filename, size, sha in FILES:
            if shutil.disk_usage(root).free < 48 * 1024**3:
                raise Stop("free disk fell below 48 GiB")
            path = root / "models" / filename
            download(path, size, sha, sources(filename))
            os.chown(path, uid, gid)
            os.chmod(path, 0o600)
        fixture = root / "fixtures" / "jfk.flac"
        download(fixture, FIXTURE_SIZE, FIXTURE_SHA, [FIXTURE_URL])
        os.chown(fixture, uid, gid)
        os.chmod(fixture, 0o600)
        build = root / "build"
        write_owned(build / "Dockerfile", DOCKERFILE, uid, gid)
        write_owned(build / "smoke.py", SMOKE, uid, gid)
        stage("build_isolated_asr_image")
        found = run(["docker", "image", "inspect", "--format", '{{json .Config.Labels}}', IMAGE], check=False)
        if found.returncode == 0:
            labels = json.loads(found.stdout) or {}
            if labels.get("org.otherworld.court-prep") != PROFILE or labels.get("org.otherworld.court-build") != BUILD_HASH:
                raise Stop("existing image label mismatch; not overwritten")
        else:
            run(["docker", "build", "--pull", "--label", "org.otherworld.court-build=" + BUILD_HASH,
                 "--tag", IMAGE, str(build)], timeout=7200, capture=False)
        status["image_id"] = run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]).stdout.strip()
        status["build_sha256"] = BUILD_HASH
        stage("recheck_selected_gpu")
        if shutil.disk_usage(root).free < 48 * 1024**3 or available_ram_kib() < 20 * 1024**2:
            raise Stop("host disk/RAM reserve no longer sufficient after preparation")
        before = gpu_info(args.gpu)
        if before["uuid"] != initial["uuid"]:
            raise Stop("GPU identity changed; refusing to run")
        require_idle_gpu(before)
        # Full hashes checked above. No network is available to the inference container.
        result_path = root / "reports" / "smoke-result.json"
        if result_path.is_symlink():
            raise Stop("unsafe report path")
        if result_path.exists():
            result_path.rename(result_path.with_name("smoke-result.previous-" + str(time.time_ns()) + ".json"))
        samples = []
        done = Event()
        def monitor():
            while not done.is_set():
                try:
                    samples.append(gpu_info(args.gpu))
                except Exception:
                    pass
                done.wait(.5)
        monitor_thread = Thread(target=monitor, daemon=True)
        stage("short_gpu_asr_smoke")
        monitor_thread.start()
        try:
            run(smoke_command(root, before["uuid"], uid, gid, name), timeout=900, capture=False)
        finally:
            done.set()
            monitor_thread.join(timeout=20)
            clean_owned_container(name)
        if not result_path.is_file():
            raise Stop("inference exited without a report")
        result = json.loads(result_path.read_text("utf-8"))
        if result.get("status") != "ASR_RUNTIME_TEST_PASSED" or result.get("input_sha256") != FIXTURE_SHA:
            raise Stop("invalid smoke result")
        # The container has exited, so its CUDA context is gone; observe rather than promise an exact memory count.
        time.sleep(2)
        status["gpu_after"] = gpu_info(args.gpu)
        status["peak_selected_gpu_used_mib"] = max((s["used_mib"] for s in samples), default=None)
        status["gpu_memory_sampling"] = "~0.5-second sampling; may miss brief peaks; entire selected GPU, not a hard quota"
        status["asr"] = {k: result[k] for k in ["audio_seconds", "load_seconds", "inference_seconds", "compute_type"]}
        status["status"] = "passed"
        status["finished_at"] = time.time()
        stage("complete")
        print("\nCOURT_ASR_PREPARE_OK", flush=True)
        print("MODEL=Whisper large-v3; COMPUTE=int8_float16; GPU_INDEX=" + str(args.gpu), flush=True)
        print("TIMING=" + json.dumps(status["asr"]), flush=True)
        print("GPU_MEMORY_MIB=" + json.dumps({"before": before["used_mib"], "sampled_peak": status["peak_selected_gpu_used_mib"], "after": status["gpu_after"]["used_mib"]}), flush=True)
        print("SITE_NOT_DEPLOYED; CHINESE_COURT_QUALITY_NOT_EVALUATED; GLM_NOT_CALLED", flush=True)
        print("REPORT=" + str(root / "status.json"), flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        status["status"] = "failed" if not isinstance(exc, KeyboardInterrupt) else "interrupted"
        status["error"] = str(exc) if isinstance(exc, Stop) else type(exc).__name__
        status["finished_at"] = time.time()
        write_owned(root / "status.json", json.dumps(status, ensure_ascii=False, indent=2), uid, gid)
        # Only our uniquely named, correctly labelled test container can be removed.
        try:
            clean_owned_container(name)
        except Exception:
            pass
        print("COURT_ASR_PREPARE_STOP: " + status["error"], flush=True)
        print("Cached downloads/builds retained. Fix the reported cause, then rerun the same command.", flush=True)
        return 1
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as exc:
        print("COURT_ASR_PREPARE_STOP: " + str(exc), flush=True)
        sys.exit(1)
