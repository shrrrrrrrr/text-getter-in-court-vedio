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
    access_db: Path | None = None
    allow_http: bool = False
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost', '[::1]', 'testserver')
    worker_subprocess: bool = False
    gpu_uuid: str = ''
    platform_public_key: Path | None = None

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("COURT_DATA_DIR", str(ROOT / ".state" / "tasks"))).resolve(),
            model=os.getenv("COURT_ASR_MODEL", "large-v3"),
            device=os.getenv("COURT_DEVICE", "cpu"),
            compute_type=os.getenv("COURT_COMPUTE_TYPE", "int8"),
            allow_download=os.getenv("COURT_ALLOW_MODEL_DOWNLOAD", "0") == "1",
            access_db=Path(os.environ['COURT_ACCESS_DB']).resolve() if os.getenv('COURT_ACCESS_DB') else None,
            allow_http=os.getenv('COURT_ALLOW_CAMPUS_HTTP', '0') == '1',
            allowed_hosts=tuple(h.strip() for h in os.getenv('COURT_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1]').split(',') if h.strip()),
            worker_subprocess=os.getenv('COURT_WORKER_SUBPROCESS', '0') == '1',
            gpu_uuid=os.getenv('COURT_GPU_UUID', ''),
            platform_public_key=Path(os.environ['COURT_PLATFORM_PUBLIC_KEY']).resolve() if os.getenv('COURT_PLATFORM_PUBLIC_KEY') else None,
            max_upload_bytes=int(os.getenv('COURT_MAX_UPLOAD_BYTES', str(4 * 1024**3))),
        )
