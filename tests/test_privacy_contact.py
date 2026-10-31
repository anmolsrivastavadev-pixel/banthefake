"""BTF_PRIVACY_CONTACT / BTF_ENV: the privacy notice contact is configuration, validated at startup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, is_placeholder_contact, validate_privacy_contact
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_email_contact_substituted_everywhere(make_client):
    res = make_client(privacy_contact="privacy@banthefake.org").get("/privacy.html")
    assert res.status_code == 200
    html = res.text
    assert html.count('href="mailto:privacy@banthefake.org"') == 2
    assert "YOUR-DOMAIN" not in html
    assert "{{" not in html
    assert "Contact not configured" not in html
    assert "config-warning" not in html


def test_https_url_contact_is_linked_directly(make_client):
    url = "https://banthefake.org/contact?topic=privacy&lang=en"
    html = make_client(privacy_contact=url).get("/privacy.html").text
    escaped = "https://banthefake.org/contact?topic=privacy&amp;lang=en"
    assert html.count(f'href="{escaped}"') == 2
    assert f'<span class="mono">{escaped}</span>' in html
    assert "mailto:" not in html
    assert "&lang" not in html.replace("&amp;lang", "")  # ampersand is HTML-escaped


def test_contact_from_env(monkeypatch):
    monkeypatch.setenv("BTF_PRIVACY_CONTACT", "  dpo@banthefake.org ")
    monkeypatch.setenv("BTF_ENV", "Production")
    settings = Settings.from_env()
    assert settings.privacy_contact == "dpo@banthefake.org"
    assert settings.env == "production"
    settings.check()


@pytest.mark.parametrize(
    "value",
    [
        '"><script>alert(1)</script>',
        'privacy@example.org"><script>',
        "javascript:alert(1)",
        "http://banthefake.org/privacy",  # https only
        "https://banthefake.org/a b",
        "https://banthefake.org/<x>",
        "https://banthefake.org/'\"",
        "mailto:privacy@banthefake.org",
        "privacy@banthefake.org\nBcc: x@y.org",
        "privacy at banthefake.org",
        "a@b",
        "x" * 250 + "@a.org",
    ],
)
def test_unsafe_or_malformed_contact_rejected_at_startup(make_client, value):
    with pytest.raises(ConfigError, match="BTF_PRIVACY_CONTACT"):
        make_client(privacy_contact=value)
    with pytest.raises(ConfigError):
        validate_privacy_contact(value)


def test_unsafe_contact_rejected_even_in_development(tmp_path):
    with pytest.raises(ConfigError):
        create_app(Settings(db_path=tmp_path / "x.db", privacy_contact='"><script>'))
    assert not (tmp_path / "x.db").exists()  # nothing opened before validation


def test_unset_contact_in_development_shows_placeholder_and_notice(client):
    html = client.get("/privacy.html").text
    assert 'href="mailto:privacy@YOUR-DOMAIN.example"' in html
    assert 'class="config-warning" role="note"' in html
    assert "Contact not configured" in html


def test_placeholder_contact_in_development_also_shows_notice(make_client):
    html = make_client(privacy_contact="privacy@YOUR-DOMAIN.example").get("/privacy.html").text
    assert "Contact not configured" in html


@pytest.mark.parametrize(
    "contact",
    [None, "privacy@YOUR-DOMAIN.example", "privacy@example.org", "ops@petition.example.com",
     "https://example.net/privacy", "a@foo.test", "a@localhost.localhost"],
)
def test_production_refuses_unset_or_placeholder_contact(make_client, contact):
    assert is_placeholder_contact(contact)
    with pytest.raises(ConfigError, match="BTF_ENV=production"):
        make_client(env="production", privacy_contact=contact)


def test_production_with_real_contact_starts(make_client):
    res = make_client(env="production", privacy_contact="privacy@banthefake.org").get("/privacy.html")
    assert res.status_code == 200
    assert "Contact not configured" not in res.text


def test_unknown_env_rejected(make_client):
    with pytest.raises(ConfigError, match="BTF_ENV"):
        make_client(env="prod")


def test_served_page_keeps_security_headers_and_no_inline_code(make_client):
    res = make_client(privacy_contact="privacy@banthefake.org").get("/privacy.html")
    assert res.headers["content-type"].startswith("text/html")
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert res.headers["cache-control"] == "no-cache"
    assert "<script" not in res.text and "<style" not in res.text


def test_head_privacy_page(client):
    assert client.head("/privacy.html").status_code == 200


def test_template_not_served_raw_from_static(client):
    assert not (ROOT / "static" / "privacy.html").exists()
    assert client.get("/static/privacy.html").status_code == 404
    assert "{{" not in client.get("/privacy.html").text


def _import_app(tmp_path, **env):
    full = {k: v for k, v in os.environ.items() if not k.startswith("BTF_")}
    full.update(BTF_DB_PATH=str(tmp_path / "proc.db"), BTF_MODEL="off", **env)
    return subprocess.run(
        [sys.executable, "-c", "import app.main"], cwd=ROOT, env=full, capture_output=True, text=True,
        timeout=120,
    )


def test_production_without_contact_exits_nonzero_with_clear_error(tmp_path):
    proc = _import_app(tmp_path, BTF_ENV="production")
    assert proc.returncode == 1
    assert "Refusing to start" in proc.stderr and "BTF_PRIVACY_CONTACT" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_malicious_contact_exits_nonzero(tmp_path):
    proc = _import_app(tmp_path, BTF_PRIVACY_CONTACT='"><script>alert(1)</script>')
    assert proc.returncode == 1
    assert "BTF_PRIVACY_CONTACT must be" in proc.stderr


def test_production_with_contact_imports(tmp_path):
    proc = _import_app(tmp_path, BTF_ENV="production", BTF_PRIVACY_CONTACT="privacy@banthefake.org")
    assert proc.returncode == 0, proc.stderr
