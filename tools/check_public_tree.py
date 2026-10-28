"""Check staged public files. Run after git add; complements manual diff review."""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
names = subprocess.check_output(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root).decode().split("\0")
errors = []
for name in filter(None, names):
    if any(part in {".state", ".venv", "node_modules", "__pycache__"} for part in Path(name).parts):
        errors.append(name + ": runtime/private path")
    raw = subprocess.check_output(["git", "show", ":" + name], cwd=root)
    if name.endswith(".woff2"):
        import hashlib
        if name != "static/fonts/ChillReunion-Round.woff2" or hashlib.sha256(raw).hexdigest() != "4f4e36dd0601c5fa325c6634649494767e9e7a0da8cc7b1fc9496ba1f7438288":
            errors.append(name + ": unrecognized binary")
        continue
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        errors.append(name + ": binary file")
        continue
    patterns = [r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY", r"\b(?:sk-|AKID)[A-Za-z0-9_-]{20,}", r"(?:SECRET_KEY|HF_TOKEN)[ \t]*=[ \t]*['\"]?[A-Za-z0-9_-]{16,}", r"(?:C:[\\/]Users[\\/]|/[h]ome/)[^\s\"<>]+"]
    if any(re.search(p, text) for p in patterns):
        errors.append(name + ": possible credential or private path")
if errors:
    raise SystemExit("\n".join(errors))
print(f"Public staged-file check passed: {len(list(filter(None, names)))} files. Review git diff --cached manually too.")
