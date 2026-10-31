import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/banthefake.db")
    max_upload_mb: int = 50
    scan_rate: int = 10
    scan_concurrency: int = 2  # scans decoding media at the same time (memory bound)
    scan_wait_s: float = 30.0  # how long a scan may queue for a slot before 503 "busy"
    sign_rate: int = 5
    petition_goal: int = 10000
    admin_token: str | None = None
    model: str = "auto"
    static_dir: Path = Path(__file__).resolve().parent.parent / "static"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("BTF_DB_PATH") or cls.db_path),
            max_upload_mb=_int("BTF_MAX_UPLOAD_MB", cls.max_upload_mb),
            scan_rate=_int("BTF_SCAN_RATE", cls.scan_rate),
            scan_concurrency=max(1, _int("BTF_SCAN_CONCURRENCY", cls.scan_concurrency)),
            scan_wait_s=_float("BTF_SCAN_WAIT_S", cls.scan_wait_s),
            sign_rate=_int("BTF_SIGN_RATE", cls.sign_rate),
            petition_goal=_int("BTF_PETITION_GOAL", cls.petition_goal),
            admin_token=os.environ.get("BTF_ADMIN_TOKEN") or None,
            model=(os.environ.get("BTF_MODEL") or cls.model).strip().lower(),
        )
