import atexit
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

# `app.main` builds a module-level app at import time, which opens BTF_DB_PATH: point it at a throwaway
# directory that is removed when the test run ends.
_IMPORT_DB_DIR = Path(tempfile.mkdtemp(prefix="btf-test-"))
atexit.register(shutil.rmtree, _IMPORT_DB_DIR, ignore_errors=True)
os.environ.setdefault("BTF_DB_PATH", str(_IMPORT_DB_DIR / "import.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN = "s3cret-token"
FIXTURES = Path(__file__).parent / "fixtures"


def pytest_collection_modifyitems(config, items):
    from app.scan import model

    if model.deps_installed():
        return
    skip = pytest.mark.skip(reason="ml extra (torch + transformers) not installed: uv sync --extra ml")
    for item in items:
        if "ml" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _model_off_unless_ml(request, monkeypatch):
    """Keep regular tests fast and deterministic: the detector only runs in tests marked `ml`."""
    if request.node.get_closest_marker("ml") is None:
        monkeypatch.setenv("BTF_MODEL", "off")
    else:
        monkeypatch.delenv("BTF_MODEL", raising=False)


@pytest.fixture
def make_client(tmp_path: Path) -> Callable[..., TestClient]:
    def make(**overrides) -> TestClient:
        base = Settings(db_path=tmp_path / "test.db", sign_rate=100, scan_rate=100, admin_token=ADMIN)
        return TestClient(create_app(replace(base, **overrides)))

    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
