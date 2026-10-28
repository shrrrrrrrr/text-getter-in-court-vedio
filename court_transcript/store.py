import hashlib
import json
import re
from pathlib import Path
from threading import RLock

from .schema import Task, now


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def directory(self, task_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise FileNotFoundError(task_id)
        return self.root / task_id

    def get(self, task_id: str) -> Task:
        with self.lock:
            return Task.model_validate_json((self.directory(task_id) / "task.json").read_text("utf-8"))

    def save(self, task: Task):
        with self.lock:
            directory = self.directory(task.id)
            directory.mkdir(exist_ok=True)
            task.updated_at = now()
            path = directory / "task.json"
            temp = directory / "task.json.tmp"
            temp.write_text(task.model_dump_json(indent=2), "utf-8")
            temp.replace(path)

    def all(self):
        tasks = []
        for path in self.root.glob("*/task.json"):
            try:
                tasks.append(self.get(path.parent.name))
            except (ValueError, OSError):
                continue
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def edit(self, task_id, revision, action, mutate):
        with self.lock:
            task = self.get(task_id)
            if task.state != "completed":
                raise Conflict("任务完成后才能校对")
            if task.revision != revision:
                raise Conflict("内容已被修改，请刷新后再保存；当前输入仍保留")
            before = task.model_dump(mode="json")
            mutate(task)
            task.revision += 1
            after = task.model_dump(mode="json")
            delta_before, delta_after = {"revision": before["revision"]}, {"revision": after["revision"]}
            for key in ["segments", "speakers"]:
                old = {v["id"]: v for v in before[key]}
                new = {v["id"]: v for v in after[key]}
                changed = {ident for ident in set(old) | set(new) if old.get(ident) != new.get(ident)}
                if changed:
                    delta_before[key] = {ident: old.get(ident) for ident in sorted(changed)}
                    delta_after[key] = {ident: new.get(ident) for ident in sorted(changed)}
            self.audit(task_id, action, delta_before, delta_after)
            self.save(task)
            return task

    def audit(self, task_id, action, before, after):
        path = self.directory(task_id) / "audit.jsonl"
        prev = "0" * 64
        if path.exists():
            lines = path.read_text("utf-8").splitlines()
            if lines:
                prev = json.loads(lines[-1])["hash"]
        entry = {"at": now(), "action": action, "before": before, "after": after, "prev": prev}
        raw = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["hash"] = hashlib.sha256(raw.encode()).hexdigest()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
