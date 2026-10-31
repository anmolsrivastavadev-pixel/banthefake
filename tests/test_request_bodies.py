"""Petition/erase body limits (16 KB, JSON only), malformed JSON, and the catch-all 500 handler."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN

VALID = {"name": "Ada Lovelace", "email": "ada@example.org", "location": "London", "consent": True}
AUTH = {"Authorization": f"Bearer {ADMIN}"}
JSON = {"Content-Type": "application/json"}


def count(client) -> int:
    return client.get("/api/petition/count").json()["count"]


def test_petition_body_over_16kb_413(client):
    body = json.dumps({**VALID, "pad": "x" * 17 * 1024})
    res = client.post("/api/petition", content=body, headers=JSON)
    assert res.status_code == 413 and "error" in res.json()
    assert count(client) == 0


def test_petition_body_just_under_limit_ok(client):
    body = json.dumps({**VALID, "pad": "x" * 15 * 1024})
    assert client.post("/api/petition", content=body, headers=JSON).status_code == 201


def test_petition_body_counted_while_streaming(client):
    # no Content-Length (chunked): the limit is enforced on the bytes actually received
    def gen():
        yield json.dumps(VALID)[:-1].encode() + b', "pad": "'
        for _ in range(64):
            yield b"x" * 1024
        yield b'"}'

    res = client.post("/api/petition", content=gen(), headers=JSON)
    assert res.status_code == 413
    assert count(client) == 0


def test_petition_lying_content_length_413(client):
    res = client.post("/api/petition", content=json.dumps(VALID), headers={**JSON, "Content-Length": "99999999"})
    assert res.status_code == 413


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""])
def test_petition_requires_json_content_type(client, ctype):
    headers = {"Content-Type": ctype} if ctype else {}
    res = client.post("/api/petition", content=json.dumps(VALID), headers=headers)
    assert res.status_code == 415 and "error" in res.json()
    assert count(client) == 0


def test_petition_json_with_charset_ok(client):
    res = client.post("/api/petition", content=json.dumps(VALID), headers={"Content-Type": "application/json; charset=utf-8"})
    assert res.status_code == 201


def test_delete_body_limits(client):
    client.post("/api/petition", json=VALID)
    big = json.dumps({"email": "ada@example.org", "pad": "x" * 20 * 1024})
    assert client.request("DELETE", "/api/petition", content=big, headers={**AUTH, **JSON}).status_code == 413
    plain = json.dumps({"email": "ada@example.org"})
    assert client.request("DELETE", "/api/petition", content=plain, headers={**AUTH, "Content-Type": "text/plain"}).status_code == 415
    assert count(client) == 1


def test_deeply_nested_json_413_under_default_cap(client):
    res = client.post("/api/petition", content="[" * 100_000 + "]" * 100_000, headers=JSON)
    assert res.status_code == 413


def test_deeply_nested_json_400(client, monkeypatch):
    # Raise the cap so the body reaches the decoder, deeper than its recursion limit (RecursionError).
    monkeypatch.setattr("app.main.MAX_JSON_BODY", 1024 * 1024, raising=False)
    res = client.post("/api/petition", content="[" * 100_000 + "]" * 100_000, headers=JSON)
    assert res.status_code == 400 and res.json() == {"error": "Invalid request body."}
    assert res.headers["x-frame-options"] == "DENY"


def test_unhandled_exception_json_500_with_security_headers(make_client, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr("app.scan.capabilities", boom)
    client = make_client()
    client = TestClient(client.app, raise_server_exceptions=False)
    res = client.get("/api/health")
    assert res.status_code == 500
    assert res.json() == {"error": "Internal server error."}
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["referrer-policy"]
