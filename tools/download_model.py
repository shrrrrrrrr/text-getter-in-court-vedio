"""Explicit model download. Print pinned snapshot path for COURT_ASR_MODEL."""
import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

parser = argparse.ArgumentParser(description="下载公开 faster-whisper 权重，不处理视频")
parser.add_argument("--repo", default="Systran/faster-whisper-small")
parser.add_argument("--revision", default="main", help="正式测评请填写固定 commit SHA；输出会显示实际快照 SHA")
parser.add_argument("--cache", default=str(Path(__file__).resolve().parent.parent / ".state" / "models"))
args = parser.parse_args()
path = snapshot_download(args.repo, revision=args.revision, cache_dir=args.cache,
                         allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"])
print("COURT_ASR_MODEL=" + str(Path(path).resolve()))
