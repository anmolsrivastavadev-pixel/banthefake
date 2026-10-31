"""Browser end-to-end fixtures: a real uvicorn server per configuration and an instrumented Playwright page.

Needs the `e2e` dependency group and Chromium:

    uv sync --group e2e && uv run --group e2e playwright install chromium
    uv run --group e2e pytest -m e2e

Every test fails if the page logs a console error, throws an uncaught exception or reports a CSP violation,
unless the test explicitly allows that message with `watch.allow(...)` (expected 4xx responses, for example).
"""

import importlib.util
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

if importlib.util.find_spec("playwright") is None:
    # Without the e2e group the default `pytest` run must not even try to import these modules.
    collect_ignore_glob = ["test_*.py"]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PORTS = range(8860, 8880)  # e2e servers only ever bind here


def _find_hf_cache() -> Path | None:
    """BTF_E2E_HF_HOME, else research/detect/hf_cache in this checkout or any parent directory."""
    if env := os.environ.get("BTF_E2E_HF_HOME"):
        return Path(env)
    for parent in (ROOT, *ROOT.parents):
        candidate = parent / "research" / "detect" / "hf_cache"
        if candidate.is_dir():
            return candidate
    return None


HF_CACHE = _find_hf_cache()

# Same-origin page, Google Fonts stylesheet stubbed out so tests never depend on a third-party network.
FONT_HOSTS = re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/")


def _free_port() -> int:
    for port in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


@dataclass
class Server:
    url: str
    proc: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")


def _wait_ready(server: Server, timeout: float, until: Callable[[str], bool] = lambda body: True) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited with {server.proc.returncode}:\n{server.log()}")
        try:
            with urllib.request.urlopen(server.url + "/api/health", timeout=2) as res:
                if until(res.read().decode()):
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"server at {server.url} not ready after {timeout}s:\n{server.log()}")


@pytest.fixture(scope="session")
def start_server(tmp_path_factory) -> Iterator[Callable[..., Server]]:
    """Start `uvicorn app.main:app` on a free port with the given BTF_* overrides; all stopped at session end."""
    servers: list[Server] = []

    def start(ready_timeout: float = 30, ready_until: Callable[[str], bool] = lambda body: True, **env: str) -> Server:
        tmp = tmp_path_factory.mktemp("server")
        port = _free_port()
        full_env = {k: v for k, v in os.environ.items() if not k.startswith(("BTF_", "HF_"))}
        full_env.update(
            BTF_MODEL="off",
            BTF_DB_PATH=str(tmp / "e2e.db"),
            BTF_SCAN_RATE="10000",
            BTF_SIGN_RATE="10000",
            BTF_SIGN_ATTEMPT_RATE="10000",
            PYTHONUNBUFFERED="1",
        )
        full_env.update(env)
        full_env = {k: v for k, v in full_env.items() if v is not None}
        log_path = tmp / "uvicorn.log"
        log_file = log_path.open("wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
             "--log-level", "warning"],
            cwd=ROOT, env=full_env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        log_file.close()
        server = Server(f"http://127.0.0.1:{port}", proc, log_path)
        servers.append(server)
        _wait_ready(server, ready_timeout, ready_until)
        return server

    yield start
    for s in servers:
        s.proc.terminate()
    for s in servers:
        try:
            s.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            s.proc.kill()


@pytest.fixture(scope="session")
def server(start_server) -> Server:
    """The default instance: detector off, generous rate limits, throwaway petition database."""
    return start_server()


@pytest.fixture(scope="session")
def base_url(server) -> str:
    # Overrides pytest-base-url's fixture, so `page.goto("/")` resolves against the e2e server.
    return server.url


@dataclass
class PageWatch:
    """Console errors, uncaught page errors and CSP violations seen by one page."""

    problems: list[str] = field(default_factory=list)
    allowed: list[re.Pattern] = field(default_factory=list)

    def allow(self, pattern: str) -> None:
        self.allowed.append(re.compile(pattern))

    def unexpected(self) -> list[str]:
        return [p for p in self.problems if not any(a.search(p) for a in self.allowed)]


CSP_LISTENER = """
document.addEventListener('securitypolicyviolation', e => {
  window.__btfReportCsp(`CSP violation: ${e.violatedDirective} blocked ${e.blockedURI || '(inline)'}`
    + ` at ${e.sourceFile || e.documentURI}:${e.lineNumber}`);
});
"""


def stub_fonts(route) -> None:
    if "fonts.googleapis.com" in route.request.url:
        route.fulfill(status=200, content_type="text/css", body="/* fonts stubbed in e2e tests */")
    else:
        route.fulfill(status=204, body="")


def instrument(page) -> PageWatch:
    watch = PageWatch()
    page.on("console", lambda msg: msg.type == "error" and watch.problems.append(f"console.error: {msg.text}"))
    page.on("pageerror", lambda exc: watch.problems.append(f"pageerror: {exc}"))
    page.expose_function("__btfReportCsp", lambda text: watch.problems.append(text))
    page.add_init_script(CSP_LISTENER)
    page.route(FONT_HOSTS, stub_fonts)
    return watch


@pytest.fixture
def watch(page) -> Iterator[PageWatch]:
    """Instrument the plugin's `page`; fail the test on any console error / page error / CSP violation."""
    w = instrument(page)
    yield w
    # Let late async work (fetches, image decodes) report before checking.
    try:
        page.wait_for_timeout(150)
    except Exception:
        pass
    bad = w.unexpected()
    assert not bad, "browser reported problems:\n" + "\n".join(bad)


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def hf_cache_has_pinned_models() -> bool:
    if HF_CACHE is None:
        return False
    try:
        from app.scan import model
    except Exception:
        return False
    if not model.deps_installed():
        return False
    for repo, rev in ((model.PRIMARY_ID, model.PRIMARY_REVISION), (model.SECONDARY_ID, model.SECONDARY_REVISION)):
        snap = HF_CACHE / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / rev
        if not (snap / "model.safetensors").exists():
            return False
    return True
