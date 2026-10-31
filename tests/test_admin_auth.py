import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import ADMIN


EXPORT = "/api/petition/export.csv"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def from_ip(app, address: str) -> TestClient:
    return TestClient(app, client=(address, 50000))


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", EXPORT, {}),
        ("DELETE", "/api/petition", {"json": {"email": "person@example.org"}}),
    ],
)
def test_wrong_token_reaches_failed_admin_limit(make_client, method, path, kwargs):
    client = make_client(admin_fail_rate=2)

    for _ in range(2):
        response = client.request(method, path, headers=auth("wrong-token"), **kwargs)
        assert response.status_code == 401

    response = client.request(method, path, headers=auth("wrong-token"), **kwargs)
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}


def test_correct_token_is_rejected_while_ip_is_limited_and_other_ip_is_unaffected(make_client):
    app = make_client(admin_fail_rate=2).app
    blocked = from_ip(app, "192.0.2.41")
    other = from_ip(app, "198.51.100.82")

    assert blocked.get(EXPORT, headers=auth("wrong-one")).status_code == 401
    assert blocked.get(EXPORT, headers=auth("wrong-two")).status_code == 401
    response = blocked.get(EXPORT, headers=auth(ADMIN))
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}

    assert other.get(EXPORT, headers=auth(ADMIN)).status_code == 200


def test_successful_admin_authentication_does_not_consume_failure_budget(make_client):
    client = make_client(admin_fail_rate=1)

    for _ in range(3):
        assert client.get(EXPORT, headers=auth(ADMIN)).status_code == 200
    assert client.get(EXPORT, headers=auth("wrong-token")).status_code == 401
    assert client.get(EXPORT, headers=auth(ADMIN)).status_code == 429


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", EXPORT, {}),
        ("DELETE", "/api/petition", {"json": {"email": "person@example.org"}}),
    ],
)
def test_unset_admin_token_always_returns_404(make_client, method, path, kwargs):
    client = make_client(admin_token=None, admin_fail_rate=1)

    for token in ("wrong-one", "wrong-two", ADMIN):
        response = client.request(method, path, headers=auth(token), **kwargs)
        assert response.status_code == 404
        assert response.json() == {"error": "Not Found"}


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("203.0.113.47", "203.0.113.0/24"),
        ("2001:db8:abcd:1234::7", "2001:db8:abcd::/48"),
    ],
)
def test_failed_auth_logs_omit_token_and_full_ip(make_client, caplog, address, masked):
    supplied_token = "do-not-log-this-admin-token"
    client = from_ip(make_client(admin_fail_rate=10).app, address)

    with caplog.at_level(logging.WARNING, logger="banthefake"):
        response = client.get(EXPORT, headers=auth(supplied_token))

    assert response.status_code == 401
    warnings = [record.getMessage() for record in caplog.records if record.name == "banthefake"]
    assert warnings == [f"Failed admin authentication from {masked}"]
    assert supplied_token not in caplog.text
    assert address not in caplog.text


def test_admin_failure_rate_setting(monkeypatch):
    monkeypatch.setenv("BTF_ADMIN_FAIL_RATE", "7")
    assert Settings.from_env().admin_fail_rate == 7
