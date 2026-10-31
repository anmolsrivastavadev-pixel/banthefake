def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["model"] in {"loaded", "off", "unavailable"}
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
    assert client.get("/styles.css").status_code == 200


def test_unknown_api_route_json_error(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert "error" in res.json()
    assert "x-frame-options" in res.headers
