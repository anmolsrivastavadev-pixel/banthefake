"""Portability: the locked c2pa-python must install on Linux, so the Docker image needs no fallback."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_c2pa_pinned_to_version_with_linux_wheels():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "c2pa-python==0.38.1" in deps
    lock = (ROOT / "uv.lock").read_text()
    block = lock[lock.index('[[package]]\nname = "c2pa-python"') + 1:]
    block = block[: block.index("[[package]]")]
    assert 'version = "0.38.1"' in block
    assert re.search(r"c2pa_python-0\.38\.1-py3-none-manylinux[^\"]*_x86_64\.whl", block)
    assert re.search(r"c2pa_python-0\.38\.1-py3-none-manylinux[^\"]*_aarch64\.whl", block)


def test_dockerfile_has_no_c2pa_fallback():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "C2PA_FALLBACK" not in dockerfile
    assert "--no-install-package c2pa-python" not in dockerfile
    readme = (ROOT / "README.md").read_text()
    assert "C2PA_FALLBACK" not in readme


def test_readme_documents_new_settings():
    readme = (ROOT / "README.md").read_text()
    for var in ("BTF_SCAN_CONCURRENCY", "BTF_SCAN_WAIT_S", "BTF_SIGN_ATTEMPT_RATE", "BTF_MAX_DECODE_PIXELS"):
        assert f"`{var}`" in readme, var
