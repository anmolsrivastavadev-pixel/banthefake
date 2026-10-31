def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["model"] in {"loaded", "loading", "off", "unavailable"}
    assert isinstance(body["c2pa"], bool)


def test_static_and_security_headers(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "BanTheFake" in res.text
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "fonts.googleapis.com" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "referrer-policy" in res.headers
    assert client.get("/app.js").status_code == 200
    css = client.get("/styles.css")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-cache"


def test_unknown_api_route_json_error(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert "error" in res.json()
    assert "x-frame-options" in res.headers


def test_camera_verdict_is_not_styled_green(client):
    """Camera EXIF can be faked or copied: the UI must not present it as a green all-clear."""
    import re

    js = client.get("/app.js").text
    css = client.get("/styles.css").text
    cls = re.search(r"VERDICT_CLASS = \{[^}]*camera: '([^']*)'", js).group(1)
    assert cls == "neutral"
    rules = " ".join(re.findall(rf"\.verdict\.{cls}[^{{]*\{{[^}}]*\}}", css))
    assert rules and "green" not in rules
    cam_tag = re.search(r"\.tag\.cam\s*\{[^}]*\}", css).group(0)
    assert "green" not in cam_tag
    assert not re.search(r"\.verdict\.ok[^{]*\{[^}]*green", css)
