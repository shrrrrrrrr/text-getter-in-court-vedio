#!/usr/bin/env python3
"""Offline ASR preparation. Uploaded wheels/model only; no server HTTPS or Docker pull."""
from __future__ import annotations

import argparse
import ctypes.util
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
import venv
from pathlib import Path, PurePosixPath
from threading import Event, Thread

try:
    from . import campus_prepare as prep
except ImportError:
    import campus_prepare as prep


def verify_bundle(root, expected_abi=None):
    root = Path(root)
    manifest_path = root / "manifest.json"
    if root.is_symlink() or manifest_path.is_symlink():
        raise prep.Stop("symlink bundle refused")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    abi = expected_abi or "cp" + str(sys.version_info.major) + str(sys.version_info.minor)
    if manifest.get("schema") != 1 or manifest.get("python_abi") != abi:
        raise prep.Stop("offline bundle Python ABI mismatch")
    records = manifest.get("files", [])
    if not isinstance(records, list) or not 10 <= len(records) <= 200:
        raise prep.Stop("invalid bundle file list")
    seen = set()
    for item in records:
        name = item.get("path", "")
        parts = PurePosixPath(name)
        if not re.fullmatch(r"[A-Za-z0-9_.+/-]+", name) or parts.is_absolute() or ".." in parts.parts or name in seen:
            raise prep.Stop("unsafe or duplicate bundle path")
        seen.add(name)
        path = root / name
        if any(p.is_symlink() for p in [path] + list(path.parents)[:len(parts.parts)]):
            raise prep.Stop("symlink bundle entry")
        if not path.is_file() or path.stat().st_size != item.get("size") or prep.digest(path) != item.get("sha256"):
            raise prep.Stop("offline bundle integrity check failed: " + name)
    required = {"tools/campus_prepare.py", "tools/campus_offline.py", "fixtures/jfk.flac"} | {"models/" + n for n, _, _ in prep.FILES}
    if not required <= seen or not any(n.startswith("wheels/pip-") for n in seen):
        raise prep.Stop("offline bundle is incomplete")
    for name, size, sha in prep.FILES:
        row = next(r for r in records if r["path"] == "models/" + name)
        if (row["size"], row["sha256"]) != (size, sha):
            raise prep.Stop("model manifest differs from fixed source")
    fixture = next(r for r in records if r["path"] == "fixtures/jfk.flac")
    if (fixture["size"], fixture["sha256"]) != (prep.FIXTURE_SIZE, prep.FIXTURE_SHA):
        raise prep.Stop("public control fixture manifest mismatch")
    return manifest


def cache_file(source, target, uid, gid):
    if target.is_symlink():
        raise prep.Stop("symlink cache refused")
    if target.exists():
        if target.stat().st_size != source.stat().st_size or prep.digest(target) != prep.digest(source):
            raise prep.Stop("existing runtime file differs; not overwritten")
        return
    # The upload and runtime usually share a filesystem: avoid another 3 GB copy.
    try:
        os.link(source, target)
    except OSError:
        partial = target.with_name(target.name + ".offline-part")
        if partial.exists() or partial.is_symlink():
            raise prep.Stop("existing unknown cache partial")
        shutil.copyfile(source, partial)
        if prep.digest(partial) != prep.digest(source):
            raise prep.Stop("cache copy checksum mismatch")
        partial.replace(target)
    os.chown(target, uid, gid)
    os.chmod(target, 0o600)


def offline_install_args(python, wheels):
    return [str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-index", "--no-deps", "--only-binary=:all:"] + [str(p) for p in sorted(wheels)]


def make_smoke_script():
    return (prep.SMOKE.replace('"/models"', 'os.environ["COURT_SMOKE_MODEL_DIR"]')
            .replace('"/fixtures/jfk.flac"', 'os.environ["COURT_SMOKE_FIXTURE"]')
            .replace('"/output/smoke-result.json"', 'os.environ["COURT_SMOKE_OUTPUT"]'))


def service_command(root, python, smoke, uuid, uid, gid, unit, description):
    args = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit=" + unit,
            "--uid=" + str(uid), "--gid=" + str(gid), "--description=" + description,
            "--property=PrivateNetwork=yes", "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict", "--property=ProtectHome=read-only",
            "--property=ReadWritePaths=" + str(root / "reports"),
            "--property=MemoryMax=12G", "--property=CPUQuota=800%", "--property=TasksMax=128", "--property=UMask=0077"]
    site = root / "venvs" / python.parent.parent.name / "lib" / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor)) / "site-packages"
    env = {"CUDA_VISIBLE_DEVICES": uuid, "NVIDIA_VISIBLE_DEVICES": uuid,
           "LD_LIBRARY_PATH": str(site / "nvidia/cublas/lib") + ":" + str(site / "nvidia/cudnn/lib"),
           "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
           "HOME": "/tmp", "XDG_CACHE_HOME": "/tmp/cache", "CUDA_CACHE_PATH": "/tmp/cuda-cache",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
           "COURT_SMOKE_MODEL_DIR": str(root / "models"), "COURT_SMOKE_FIXTURE": str(root / "fixtures/jfk.flac"),
           "COURT_SMOKE_OUTPUT": str(root / "reports/smoke-result.json")}
    args += ["--setenv=" + key + "=" + value for key, value in env.items()]
    return args + [str(python), str(smoke)]


def clean_unit(unit, description):
    result = prep.run(["systemctl", "show", unit, "--property=Description", "--value"], check=False)
    if result.returncode == 0 and result.stdout.strip() == description:
        prep.run(["systemctl", "stop", unit])


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--gpu", type=int, default=3)
    args = parser.parse_args()
    uid, gid, home = prep.identity()
    bundle = args.bundle.resolve()
    if not bundle.is_relative_to(home) or bundle == home:
        raise prep.Stop("bundle must be inside the invoking user's home")
    root = prep.prepare_directory(home, uid, gid)
    lock_path = root / "prepare.lock"
    if lock_path.is_symlink():
        raise prep.Stop("unsafe lock path")
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise prep.Stop("another preparation is active")
    status = {"profile": prep.PROFILE, "mode": "offline-venv", "status": "running", "stage": "verify_offline_bundle",
              "started_at": time.time(), "campus_site_deployed": False, "diarization_configured": False,
              "glm_called": False, "other_services_changed": False, "server_network_downloads": False}
    unit = "court-asr-offline-" + secrets.token_hex(8)
    description = "Court ASR offline smoke " + unit
    def stage(value):
        status["stage"] = value
        prep.write_owned(root / "status.json", json.dumps(status, ensure_ascii=False, indent=2), uid, gid)
        print("STAGE " + value, flush=True)
    try:
        stage("verify_offline_bundle")
        manifest = verify_bundle(bundle)
        status["bundle_manifest_sha256"] = prep.digest(bundle / "manifest.json")
        if shutil.disk_usage(root).free < 48 * 1024**3 or prep.available_ram_kib() < 20 * 1024**2:
            raise prep.Stop("disk/RAM reserve insufficient")
        for binary in ["systemd-run", "systemctl", "nvidia-smi"]:
            if not shutil.which(binary):
                raise prep.Stop("required command missing: " + binary)
        if not ctypes.util.find_library("gomp"):
            raise prep.Stop("host libgomp.so.1 missing; no global install attempted")
        before = prep.gpu_info(args.gpu)
        prep.require_idle_gpu(before)
        status["gpu_before"] = before
        stage("reuse_verified_local_assets")
        for name, _, _ in prep.FILES:
            cache_file(bundle / "models" / name, root / "models" / name, uid, gid)
        cache_file(bundle / "fixtures/jfk.flac", root / "fixtures/jfk.flac", uid, gid)
        stage("install_isolated_offline_venv")
        env_root = root / "venvs"
        if env_root.is_symlink():
            raise prep.Stop("unsafe virtual environment root")
        env_root.mkdir(mode=0o755, exist_ok=True)
        env_path = env_root / (manifest["python_abi"] + "-" + status["bundle_manifest_sha256"][:12])
        marker = env_path / "court-owner.json"
        expected = {"profile": prep.PROFILE, "manifest_sha256": status["bundle_manifest_sha256"], "uid": uid}
        if env_path.exists():
            if env_path.is_symlink() or marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text("utf-8")) != expected:
                raise prep.Stop("unrecognized virtual environment; not modified")
        else:
            env_path.mkdir(mode=0o755)
            marker.write_text(json.dumps(expected), "utf-8")
        venv.EnvBuilder(with_pip=False, symlinks=False).create(env_path)
        python = env_path / "bin/python3"
        wheels = [bundle / row["path"] for row in manifest["files"] if row["path"].startswith("wheels/")]
        pip_wheels = [p for p in wheels if p.name.startswith("pip-")]
        if len(pip_wheels) != 1:
            raise prep.Stop("exactly one verified pip bootstrap wheel required")
        install_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home), "PYTHONPATH": str(pip_wheels[0]), "PYTHONNOUSERSITE": "1"}
        # No index, no dependency fetch, no host pip. All inputs verified above.
        result = __import__("subprocess").run(offline_install_args(python, wheels), env=install_env, stdin=__import__("subprocess").DEVNULL, timeout=1800)
        if result.returncode:
            raise prep.Stop("offline venv installation failed")
        result = __import__("subprocess").run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "check"], env=install_env, stdin=__import__("subprocess").DEVNULL, timeout=120)
        if result.returncode:
            raise prep.Stop("offline dependency check failed")
        smoke = root / "reports/offline-smoke.py"
        prep.write_owned(smoke, make_smoke_script(), uid, gid)
        stage("recheck_selected_gpu")
        current = prep.gpu_info(args.gpu)
        if current["uuid"] != before["uuid"]:
            raise prep.Stop("GPU identity changed")
        prep.require_idle_gpu(current)
        if prep.available_ram_kib() < 20 * 1024**2:
            raise prep.Stop("host RAM reserve fell below threshold")
        report = root / "reports/smoke-result.json"
        if report.is_symlink():
            raise prep.Stop("unsafe report path")
        if report.exists():
            report.rename(report.with_name("smoke-result.previous-" + str(time.time_ns()) + ".json"))
        done, samples = Event(), []
        def monitor():
            while not done.is_set():
                try:
                    samples.append(prep.gpu_info(args.gpu)["used_mib"])
                except Exception:
                    pass
                done.wait(.5)
        thread = Thread(target=monitor, daemon=True)
        stage("offline_gpu_smoke")
        thread.start()
        try:
            prep.run(service_command(root, python, smoke, before["uuid"], uid, gid, unit, description), timeout=900, capture=False)
        finally:
            done.set()
            thread.join(timeout=20)
            clean_unit(unit, description)
        result = json.loads(report.read_text("utf-8"))
        if result.get("status") != "ASR_RUNTIME_TEST_PASSED" or result.get("input_sha256") != prep.FIXTURE_SHA:
            raise prep.Stop("invalid offline smoke result")
        time.sleep(2)
        status.update(status="passed", gpu_after=prep.gpu_info(args.gpu), sampled_peak_used_mib=max(samples, default=None),
                      asr={k: result[k] for k in ["audio_seconds", "load_seconds", "inference_seconds", "compute_type"]}, finished_at=time.time())
        stage("complete")
        print("\nCOURT_ASR_PREPARE_OK", flush=True)
        print("MODE=offline-venv; SERVER_HTTPS_NOT_USED; GLM_NOT_CALLED", flush=True)
        print("TIMING=" + json.dumps(status["asr"]), flush=True)
        print("GPU_MEMORY_MIB=" + json.dumps({"before": before["used_mib"], "sampled_peak": status["sampled_peak_used_mib"], "after": status["gpu_after"]["used_mib"]}), flush=True)
        print("SITE_NOT_DEPLOYED; CHINESE_COURT_QUALITY_NOT_EVALUATED", flush=True)
        print("REPORT=" + str(root / "status.json"), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        status.update(status="failed", error=str(exc) if isinstance(exc, prep.Stop) else type(exc).__name__, finished_at=time.time())
        stage(status["stage"])
        try:
            clean_unit(unit, description)
        except Exception:
            pass
        print("COURT_ASR_PREPARE_STOP: " + status["error"], flush=True)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except prep.Stop as exc:
        print("COURT_ASR_PREPARE_STOP: " + str(exc), flush=True)
        sys.exit(1)
