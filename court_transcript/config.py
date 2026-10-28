import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = ROOT / ".state" / "tasks"
    max_upload_bytes: int = 4 * 1024**3
    max_duration: float = 8 * 3600
    model: str = "large-v3"
    device: str = "cpu"
    compute_type: str = "int8"
    allow_download: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("COURT_DATA_DIR", str(ROOT / ".state" / "tasks"))).resolve(),
            model=os.getenv("COURT_ASR_MODEL", "large-v3"),
            device=os.getenv("COURT_DEVICE", "cpu"),
            compute_type=os.getenv("COURT_COMPUTE_TYPE", "int8"),
            allow_download=os.getenv("COURT_ALLOW_MODEL_DOWNLOAD", "0") == "1",
        )
