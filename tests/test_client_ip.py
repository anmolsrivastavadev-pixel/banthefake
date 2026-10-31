"""Operator-selected client-IP headers for rate limiting behind managed edges."""

from fastapi.testclient import TestClient

from tests.conftest import ADMIN


def _from_peer(app, address: str = "192.0.2.10") -> TestClient:
    return TestClient(app, client=(address, 50000))


def _scan(client: TestClient, header_value: str):
    return client.post(
        "/api/scan",
        headers={"Fly-Client-IP": header_value},
        files={"file": ("bad.jpg", b"not an image", "image/jpeg")},
    )


def test_client_ip_header_is_ignored_unless_configured(make_client):
    client = _from_peer(make_client(scan_rate=1).app)

    assert _scan(client, "203.0.113.1").status_code == 415
    assert _scan(client, "203.0.113.2").status_code == 429


def test_configured_header_separates_rate_limit_buckets_and_normalizes_ipv6(make_client):
    client = _from_peer(make_client(scan_rate=1, client_ip_header="Fly-Client-IP").app)

    assert _scan(client, "2001:0db8:0000:0000:0000:0000:0000:0001").status_code == 415
    assert _scan(client, "2001:db8::1").status_code == 429
    assert _scan(client, "2001:db8::2").status_code == 415


def test_invalid_configured_header_values_fall_back_to_socket_peer(make_client):
    client = _from_peer(make_client(scan_rate=1, client_ip_header="Fly-Client-IP").app)

    assert _scan(client, "not-an-ip").status_code == 415
    assert _scan(client, "203.0.113.1, 198.51.100.2").status_code == 429


def test_petition_limiters_use_configured_header(make_client):
    client = _from_peer(
        make_client(sign_rate=1, sign_attempt_rate=2, client_ip_header="Fly-Client-IP").app
    )

    def sign(address: str, email: str):
        return client.post(
            "/api/petition",
            headers={"Fly-Client-IP": address},
            json={"name": "Person", "email": email, "consent": True},
        )

    assert sign("203.0.113.1", "one@example.org").status_code == 201
    assert sign("203.0.113.2", "two@example.org").status_code == 201
    assert sign("203.0.113.1", "three@example.org").status_code == 429


def test_admin_failure_limiter_uses_configured_header(make_client):
    client = _from_peer(
        make_client(admin_fail_rate=1, client_ip_header="Fly-Client-IP").app
    )
    wrong = {"Authorization": "Bearer wrong", "Fly-Client-IP": "203.0.113.1"}
    first_correct = {"Authorization": f"Bearer {ADMIN}", "Fly-Client-IP": "203.0.113.1"}
    other_correct = {"Authorization": f"Bearer {ADMIN}", "Fly-Client-IP": "203.0.113.2"}

    assert client.get("/api/petition/export.csv", headers=wrong).status_code == 401
    assert client.get("/api/petition/export.csv", headers=first_correct).status_code == 429
    assert client.get("/api/petition/export.csv", headers=other_correct).status_code == 200


def test_client_ip_header_setting(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("BTF_CLIENT_IP_HEADER", " Fly-Client-IP ")
    assert Settings.from_env().client_ip_header == "Fly-Client-IP"
