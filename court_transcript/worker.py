"""One task per process, including its CUDA context. Never launch an HTTP server here."""
import argparse
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .runner import Runner
from .store import Store


class FileEvent:
    def __init__(self, path):
        self.path = path

    def is_set(self):
        return self.path.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', required=True)
    args = parser.parse_args()
    settings = replace(Settings.from_env(), worker_subprocess=False)
    store = Store(settings.data_dir)
    event = FileEvent(store.directory(args.task_id) / 'worker.cancel')
    runner = Runner(store, settings)
    runner.defer_completion = True
    try:
        runner.run(args.task_id, event)
    finally:
        runner.shutdown()


if __name__ == '__main__':
    main()
