"""Opt-in CORS (BTF_CORS_ORIGINS) for the browser extension: exact origins, scanner and health only."""

import pytest

from app.config import ConfigError, Settings
from app.main import CSP, create_app
from tests import media

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
OTHER = "https://evil.example"


def _scan(client, origin: str | None = None, data: bytes | None = None):
    headers = {"Origin": origin} if origin else {}
    return client.post("/api/scan", headers=headers, files={"file": ("x.jpg", data or media.jpeg(), "image/jpeg")})


def _preflight(client, path: str = "/api/scan", origin: str = EXT, method: str = "POST"):
    return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method})


def test_default_is_unchanged_no_cors_headers(client):
    res = _scan(client, EXT)
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers
    assert "origin" not in res.headers.get("vary", "").lower()
    assert _preflight(client).status_code == 405


def test_default_settings_have_no_origins(monkeypatch):
    monkeypatch.delenv("BTF_CORS_ORIGINS", raising=False)
    assert Settings.from_env().cors_origins == ()


def test_env_parsing_trims_and_drops_empties(monkeypatch):
    monkeypatch.setenv("BTF_CORS_ORIGINS", f" {EXT} , ,http://localhost:8000,")
    assert Settings.from_env().cors_origins == (EXT, "http://localhost:8000")


def test_allowed_origin_can_read_scan_and_errors(make_client):
    client = make_client(cors_origins=(EXT,), scan_rate=2)
    ok = _scan(client, EXT)
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == EXT
    assert "Origin" in ok.headers["vary"]
    assert "access-control-allow-credentials" not in ok.headers
    # The site's strict headers are kept on cross-origin responses.
    assert ok.headers["content-security-policy"] == CSP
    assert ok.headers["x-frame-options"] == "DENY"

    unsupported = _scan(client, EXT, b"not an image")
    assert unsupported.status_code == 415
    assert unsupported.headers["access-control-allow-origin"] == EXT

    limited = _scan(client, EXT)
    assert limited.status_code == 429
    assert limited.headers["access-control-allow-origin"] == EXT


def test_other_origin_gets_no_allow_header(make_client):
    client = make_client(cors_origins=(EXT,))
    res = _scan(client, OTHER)
    assert res.status_code == 200  # CORS only controls who may read the response, the scan itself is unchanged
    assert "access-control-allow-origin" not in res.headers
    assert "Origin" in res.headers["vary"]
    assert _preflight(client, origin=OTHER).status_code == 405


def test_preflight_for_allowed_origin(make_client):
    client = make_client(cors_origins=(EXT,))
    res = _preflight(client)
    assert res.status_code == 204
    assert res.headers["access-control-allow-origin"] == EXT
    assert res.headers["access-control-allow-methods"] == "POST"
    assert "access-control-allow-headers" not in res.headers
    assert "access-control-allow-credentials" not in res.headers
    # Wrong method for the path: not a preflight we answer.
    assert _preflight(client, method="DELETE").status_code == 405


def test_health_is_readable_by_allowed_origin(make_client):
    client = make_client(cors_origins=(EXT,))
    res = client.get("/api/health", headers={"Origin": EXT})
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == EXT
    assert _preflight(client, "/api/health", method="GET").status_code == 204


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/petition", {"json": {"name": "A", "email": "a@b.org", "consent": True}}),
        ("get", "/api/petition/count", {}),
        ("get", "/", {}),
        ("get", "/privacy.html", {}),
    ],
)
def test_other_endpoints_never_get_cors(make_client, method, path, kwargs):
    client = make_client(cors_origins=(EXT,))
    res = getattr(client, method)(path, headers={"Origin": EXT}, **kwargs)
    assert "access-control-allow-origin" not in res.headers
    assert _preflight(client, path).status_code in (404, 405)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.org",
        "http://localhost:8000",
        "http://127.0.0.1:8888",
        "https://[::1]:8443",
        EXT,
        "moz-extension://0b5e2f4c-7a2e-4d4e-9a55-0a1b2c3d4e5f",
    ],
)
def test_valid_origins_accepted(make_client, origin):
    make_client(cors_origins=(origin,))


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://example.org/",
        "https://example.org/api",
        "https://*.example.org",
        "example.org",
        "ftp://example.org",
        "file://",
        "https://Example.org",
        "https://example.org:0",
        "https://example.org:99999",
        "https://user@example.org",
        "https://example..org",
        "chrome-extension://abc:443",
        "https://example.org?x=1",
    ],
)
def test_invalid_origins_refuse_to_start(tmp_path, origin):
    with pytest.raises(ConfigError, match="BTF_CORS_ORIGINS"):
        create_app(Settings(db_path=tmp_path / "t.db", cors_origins=(origin,)))
