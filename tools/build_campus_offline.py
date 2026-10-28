"""Prepare a Linux ASR bundle on the Windows computer, with normal TLS verification."""
from __future__ import annotations

import argparse
import base64
import email
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import httpx
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import cpython_tags, compatible_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

try:
    from . import campus_prepare as prep
except ImportError:
    import campus_prepare as prep

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / ".state/offline-assets"
REQUIREMENTS = ["pip==25.3", "faster-whisper==1.2.1", "av==16.1.0", "ctranslate2==4.8.4", "numpy>=2,<3",
                "nvidia-cublas-cu12==12.8.4.1", "nvidia-cudnn-cu12==9.13.1.26", "huggingface-hub==1.30.0"]


def linux_environment(version):
    return {"implementation_name": "cpython", "implementation_version": version + ".0", "os_name": "posix",
            "platform_machine": "x86_64", "platform_release": "", "platform_system": "Linux", "platform_version": "",
            "python_full_version": version + ".0", "platform_python_implementation": "CPython", "python_version": version,
            "sys_platform": "linux", "extra": ""}


def wheel_metadata(path):
    with zipfile.ZipFile(path) as wheel:
        rows = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
        if len(rows) != 1 or wheel.getinfo(rows[0]).file_size > 2 * 1024**2:
            raise RuntimeError("invalid wheel metadata")
        return email.message_from_bytes(wheel.read(rows[0]))


def dependency_closure(directory, version, requirements=REQUIREMENTS):
    """Evaluate markers for Linux/target Python, never for the Windows packaging host."""
    major, minor = map(int, version.split("."))
    abi = "cp" + str(major) + str(minor)
    platforms = ["manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_27_x86_64", "manylinux_2_28_x86_64"]
    supported = set(cpython_tags((major, minor), abis=[abi], platforms=platforms)) | set(compatible_tags((major, minor), interpreter=abi, platforms=platforms))
    inventory = {}
    for path in sorted(directory.glob("*.whl")):
        name, pkg_version, _, tags = parse_wheel_filename(path.name)
        fields = wheel_metadata(path)
        if not tags & supported or Version(version + ".0") not in SpecifierSet(fields.get("Requires-Python", "")):
            continue
        inventory.setdefault(name, []).append((pkg_version, path, fields))
    environment = linux_environment(version)
    constraints, extras, selected, missing, expanded = {}, {}, {}, {}, set()
    queue = [Requirement(r) for r in requirements]
    while queue:
        req = queue.pop(0)
        if req.url:
            raise RuntimeError("direct dependency URL refused")
        key = canonicalize_name(req.name)
        group = constraints.setdefault(key, [])
        if str(req) not in {str(r) for r in group}:
            group.append(req)
        extras.setdefault(key, set()).update(req.extras)
        choices = [item for item in inventory.get(key, []) if all(item[0] in rule.specifier for rule in group)]
        if not choices:
            missing[key] = key + ",".join(str(rule.specifier) for rule in group if str(rule.specifier))
            selected.pop(key, None)
            continue
        choice = max(choices, key=lambda item: item[0])
        selected[key] = choice
        missing.pop(key, None)
        signature = (key, choice[0], tuple(sorted(extras[key])))
        if signature in expanded:
            continue
        expanded.add(signature)
        for raw in choice[2].get_all("Requires-Dist", []):
            child = Requirement(raw)
            if child.marker and not any(child.marker.evaluate({**environment, "extra": extra}) for extra in [""] + list(extras[key])):
                continue
            child.marker = None
            queue.append(child)
    return [row[1] for _, row in sorted(selected.items())], list(missing.values())


def fetch(client, target, size, sha, urls):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError("symlink asset refused")
    if target.exists():
        if target.stat().st_size == size and prep.digest(target) == sha:
            print("CACHED " + target.name, flush=True)
            return
        raise RuntimeError("existing asset differs: " + target.name)
    for index, url in enumerate(urls):
        part = target.with_name(target.name + ".source-" + str(index) + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if part.is_symlink() or offset > size:
            raise RuntimeError("invalid local partial")
        try:
            print("FETCH " + target.name, flush=True)
            with client.stream("GET", url, headers={"Range": "bytes=" + str(offset) + "-"} if offset else {}) as response:
                response.raise_for_status()
                if offset and response.status_code == 200:
                    offset = 0
                if offset and not response.headers.get("content-range", "").startswith("bytes " + str(offset) + "-"):
                    raise RuntimeError("invalid range response")
                count, last = offset, time.monotonic()
                with part.open("ab" if offset else "wb") as stream:
                    for chunk in response.iter_bytes(2 * 1024 * 1024):
                        count += len(chunk)
                        if count > size:
                            raise RuntimeError("download exceeded manifest size")
                        stream.write(chunk)
                        if time.monotonic() - last > 15:
                            print(target.name + " " + str(round(100 * count / size, 1)) + "%", flush=True)
                            last = time.monotonic()
            if part.stat().st_size != size or prep.digest(part) != sha:
                raise RuntimeError("download checksum mismatch")
            part.replace(target)
            return
        except (httpx.HTTPError, OSError):
            print("SOURCE_UNAVAILABLE " + target.name, flush=True)
    raise RuntimeError("no normally verified HTTPS source: " + target.name)


def wheel_provenance(client, path):
    fields = wheel_metadata(path)
    name, version = fields["Name"], fields["Version"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or "") or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version or ""):
        raise RuntimeError("invalid distribution metadata")
    response = client.get("https://pypi.org/pypi/" + name + "/" + version + "/json")
    response.raise_for_status()
    rows = [r for r in response.json()["urls"] if r["filename"] == path.name]
    if len(rows) != 1 or rows[0]["size"] != path.stat().st_size or rows[0]["digests"]["sha256"] != prep.digest(path):
        raise RuntimeError("wheel differs from PyPI release: " + path.name)
    return {"name": name, "version": version, "url": rows[0]["url"], "sha256": rows[0]["digests"]["sha256"]}


def file_row(path, name):
    return {"path": name, "size": path.stat().st_size, "sha256": prep.digest(path)}


def build(version, skip_download=False):
    if not re.fullmatch(r"3\.(10|11|12|13)", version):
        raise RuntimeError("supported server Python versions: 3.10 through 3.13")
    abi = "cp" + version.replace(".", "")
    ASSETS.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(ASSETS).free < 20 * 1024**3:
        raise RuntimeError("keep at least 20 GiB local disk reserve")
    wheels = ASSETS / abi / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    if not skip_download:
        subprocess.run([sys.executable, "-m", "pip", "download", "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
            "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_27_x86_64", "--platform", "manylinux_2_28_x86_64",
            "--python-version", version, "--implementation", "cp", "--abi", abi, "--dest", str(wheels)] + REQUIREMENTS, check=True)
    for _ in range(15):
        selected_wheels, missing = dependency_closure(wheels, version)
        if not missing:
            break
        print("FETCH_LINUX_MARKER_DEPENDENCIES " + ", ".join(missing), flush=True)
        subprocess.run([sys.executable, "-m", "pip", "download", "--index-url", "https://pypi.org/simple", "--no-deps", "--only-binary=:all:",
            "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_27_x86_64", "--platform", "manylinux_2_28_x86_64",
            "--python-version", version, "--implementation", "cp", "--abi", abi, "--dest", str(wheels)] + missing, check=True)
    else:
        raise RuntimeError("Linux dependency closure did not resolve")
    paths = []
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True) as client:
        for name, size, sha in prep.FILES:
            path = ASSETS / "models" / name
            fetch(client, path, size, sha, prep.sources(name))
            paths.append((path, "models/" + name))
        fixture = ASSETS / "fixtures/jfk.flac"
        fetch(client, fixture, prep.FIXTURE_SIZE, prep.FIXTURE_SHA, [prep.FIXTURE_URL])
        paths.append((fixture, "fixtures/jfk.flac"))
        provenance = []
        for path in selected_wheels:
            print("VERIFY_PYPI " + path.name, flush=True)
            provenance.append(wheel_provenance(client, path))
            paths.append((path, "wheels/" + path.name))
    if len(provenance) < 10:
        raise RuntimeError("offline wheel dependency set is incomplete")
    for name in ["campus_prepare.py", "campus_offline.py"]:
        paths.append((ROOT / "tools" / name, "tools/" + name))
    records = [file_row(path, name) for path, name in paths]
    manifest = {"schema": 1, "python_abi": abi, "platform": "linux-x86_64-glibc>=2.28", "model_revision": prep.HF_REVISION,
                "files": records, "wheel_sources": provenance, "server_network_downloads": False}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
    bundles = ROOT / ".state/offline-bundles"
    bundles.mkdir(exist_ok=True)
    archive = bundles / ("court-asr-" + abi + "-" + fingerprint[:12] + ".tar")
    receipt = archive.with_suffix(".receipt.json")
    if archive.exists() and receipt.exists():
        previous = json.loads(receipt.read_text("utf-8"))
        if previous.get("manifest_sha256") == fingerprint and previous.get("size") == archive.stat().st_size and previous.get("sha256") == prep.digest(archive):
            print("OFFLINE_BUNDLE_CACHED", flush=True)
            return previous
        raise RuntimeError("existing offline archive differs; not overwritten")
    partial = archive.with_suffix(".partial.tar")
    if partial.exists():
        raise RuntimeError("previous incomplete archive exists; preserve it for inspection")
    print("PACK_OFFLINE_BUNDLE", flush=True)
    import io
    with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mode = len(manifest_bytes), 0o600
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for path, name in paths:
            tar.add(path, arcname=name, recursive=False)
    partial.replace(archive)
    result = {"archive": str(archive), "size": archive.stat().st_size, "sha256": prep.digest(archive), "manifest_sha256": fingerprint,
              "python_abi": abi, "files": len(paths) + 1}
    receipt.write_text(json.dumps(result, indent=2), "utf-8")
    return result


def receiver_command(remote_path, sha):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_path) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise RuntimeError("unsafe receiver arguments")
    code = base64.b64encode((ROOT / "tools/receive_campus_bundle.py").read_bytes()).decode()
    # No embedded double quotes: survives legacy Windows PowerShell native-argument quoting.
    return "python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))' " + code + " '" + remote_path + "' '" + sha + "'"


def server_continue_command(upload_id, sha):
    """One block for an existing server terminal; reuse the already verified upload."""
    if not re.fullmatch(r"[a-f0-9]{12}", upload_id) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise RuntimeError("unsafe server continuation arguments")
    return '\n'.join([
        '(', 'set -eu', 'umask 077',
        "court_sha='" + sha + "'",
        'court_archive="$HOME/court-asr-upload.' + upload_id + '/bundle-$court_sha.tar"',
        'printf \'%s  %s\\n\' "$court_sha" "$court_archive" | sha256sum --check -',
        'court_payload=$(mktemp -d "$(dirname "$court_archive")/payload.XXXXXX")',
        'tar --extract --file="$court_archive" --directory="$court_payload" --no-same-owner --no-same-permissions',
        'sudo python3 "$court_payload/tools/campus_offline.py" --bundle "$court_payload" --gpu 3',
        ')', ''
    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-version", default="3.10")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--receiver-path")
    parser.add_argument("--receiver-sha")
    parser.add_argument("--server-upload-id")
    parser.add_argument("--server-sha")
    args = parser.parse_args()
    if args.server_upload_id:
        print(server_continue_command(args.server_upload_id, args.server_sha), end='')
    elif args.receiver_path:
        print(receiver_command(args.receiver_path, args.receiver_sha))
    else:
        output = build(args.python_version, args.skip_download)
        if args.result:
            args.result.write_text(json.dumps(output, indent=2), "utf-8")
        print("OFFLINE_BUNDLE_READY " + json.dumps(output), flush=True)
