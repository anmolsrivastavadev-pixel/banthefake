import csv
import io

from tests.conftest import ADMIN

VALID = {"name": "Ada Lovelace", "email": "ada@example.org", "location": "London", "consent": True}
AUTH = {"Authorization": f"Bearer {ADMIN}"}


def count(client) -> int:
    return client.get("/api/petition/count").json()["count"]


def test_sign_happy_path(client):
    assert client.get("/api/petition/count").json() == {"count": 0, "goal": 10000}
    res = client.post("/api/petition", json=VALID)
    assert res.status_code == 201
    assert count(client) == 1


def test_goal_from_settings(make_client):
    assert make_client(petition_goal=500).get("/api/petition/count").json()["goal"] == 500


def test_duplicate_email_case_insensitive(client):
    assert client.post("/api/petition", json=VALID).status_code == 201
    res = client.post("/api/petition", json={**VALID, "email": "  ADA@Example.ORG "})
    assert res.status_code == 409
    assert res.json() == {"error": "already_signed"}
    assert count(client) == 1


def test_validation_errors(client):
    bad = [
        {**VALID, "name": ""},
        {**VALID, "name": "x" * 101},
        {**VALID, "email": "not-an-email"},
        {**VALID, "email": "a" * 250 + "@ex.com"},
        {**VALID, "location": "y" * 101},
        {**VALID, "consent": False},
        {k: v for k, v in VALID.items() if k != "consent"},
        {**VALID, "consent": "true"},
    ]
    for payload in bad:
        res = client.post("/api/petition", json=payload)
        assert res.status_code == 422, payload
        assert "error" in res.json()
    assert client.post("/api/petition", content=b"not json").status_code == 400
    assert client.post("/api/petition", json=["list"]).status_code == 400
    assert count(client) == 0


def test_location_optional(client):
    payload = {k: v for k, v in VALID.items() if k != "location"}
    assert client.post("/api/petition", json=payload).status_code == 201


def test_honeypot_returns_201_without_storing(client):
    res = client.post("/api/petition", json={**VALID, "website": "http://spam.example"})
    assert res.status_code == 201
    assert count(client) == 0


def test_rate_limit(make_client):
    client = make_client(sign_rate=2)
    assert client.post("/api/petition", json={**VALID, "email": "a@example.org"}).status_code == 201
    assert client.post("/api/petition", json={**VALID, "email": "b@example.org"}).status_code == 201
    res = client.post("/api/petition", json={**VALID, "email": "c@example.org"})
    assert res.status_code == 429
    assert "error" in res.json()
    assert count(client) == 2


def test_export_requires_configured_token(make_client):
    res = make_client(admin_token=None).get("/api/petition/export.csv", headers=AUTH)
    assert res.status_code == 404


def test_export_wrong_token(client):
    assert client.get("/api/petition/export.csv").status_code == 401
    assert client.get("/api/petition/export.csv", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/petition/export.csv", headers={"Authorization": ADMIN}).status_code == 401


def test_export_csv_escapes_formulas(client):
    client.post("/api/petition", json={**VALID, "name": "=HYPERLINK(\"x\")", "location": "+1 city"})
    client.post("/api/petition", json={**VALID, "name": "-Bob", "email": "bob@example.org", "location": "@here"})
    client.post("/api/petition", json={**VALID, "name": "Plain", "email": "plain@example.org"})
    res = client.get("/api/petition/export.csv", headers=AUTH)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == ["name", "email", "location", "created_at"]
    assert rows[1][:3] == ["'=HYPERLINK(\"x\")", "ada@example.org", "'+1 city"]
    assert rows[2][:3] == ["'-Bob", "bob@example.org", "'@here"]
    assert rows[3][:3] == ["Plain", "plain@example.org", "London"]
    assert rows[3][3].endswith("+00:00")


def test_delete_signature(client):
    client.post("/api/petition", json=VALID)
    assert client.request("DELETE", "/api/petition", json={"email": "ADA@example.org"}).status_code == 401
    res = client.request("DELETE", "/api/petition", json={"email": "ADA@example.org"}, headers=AUTH)
    assert res.status_code == 200
    assert count(client) == 0
    res = client.request("DELETE", "/api/petition", json={"email": "ada@example.org"}, headers=AUTH)
    assert res.status_code == 404
    assert client.post("/api/petition", json=VALID).status_code == 201


def test_delete_without_token_configured(make_client):
    res = make_client(admin_token=None).request("DELETE", "/api/petition", json={"email": "a@b.co"})
    assert res.status_code == 404
