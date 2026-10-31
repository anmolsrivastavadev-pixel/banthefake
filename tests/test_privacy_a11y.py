"""Privacy notice page and basic accessibility guarantees of the static frontend."""

import re


def test_privacy_page_served_with_security_headers(client):
    res = client.get("/privacy.html")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "referrer-policy" in res.headers
    html = res.text
    assert "Privacy notice" in html
    assert "privacy@YOUR-DOMAIN.example" in html  # placeholder the README says to replace
    assert "Google Fonts" in html
    assert "GDPR compliant" not in html


def test_privacy_page_has_no_inline_code(client):
    """The CSP forbids inline scripts, styles and event handlers, so the page must not rely on them."""
    html = client.get("/privacy.html").text
    assert "<script" not in html
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert '<link rel="stylesheet" href="/styles.css"' in html


def test_index_links_to_privacy_from_consent_and_footer(client):
    html = client.get("/").text
    consent = re.search(r'<label class="consent">.*?</label>', html, re.S).group(0)
    assert 'href="/privacy.html"' in consent
    footer = re.search(r"<footer>.*?</footer>", html, re.S).group(0)
    assert 'href="/privacy.html"' in footer


def test_index_accessibility_basics(client):
    html = client.get("/").text
    assert 'class="skip" href="#main"' in html and 'id="main"' in html
    assert len(re.findall(r"<h1[\s>]", html)) == 1
    # Status messages are announced to screen readers.
    assert re.search(r'id="scanStatus"[^>]*role="status"', html)
    assert re.search(r'id="thanks"[^>]*role="status"', html)
    # Every visible form field has a <label for=...>, not just a placeholder.
    for field_id in ("pName", "pEmail", "pLocation", "recipient", "lname", "lplace", "letter"):
        assert f'for="{field_id}"' in html and f'id="{field_id}"' in html
    # The file input is focusable (visually hidden, not display:none).
    css = client.get("/styles.css").text
    drop_input = re.search(r"\.drop input\s*\{[^}]*\}", css).group(0)
    assert "display: none" not in drop_input
    assert ".drop:focus-within" in css
    assert "prefers-reduced-motion" in css
