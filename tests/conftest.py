import os
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("BTF_DB_PATH", str(Path(tempfile.mkdtemp(prefix="btf-test-")) / "import.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN = "s3cret-token"


@pytest.fixture
def make_client(tmp_path: Path) -> Callable[..., TestClient]:
    def make(**overrides) -> TestClient:
        base = Settings(db_path=tmp_path / "test.db", sign_rate=100, scan_rate=100, admin_token=ADMIN)
        return TestClient(create_app(replace(base, **overrides)))

    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
