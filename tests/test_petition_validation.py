"""Stricter petition validation (email syntax, invisible/control characters) and rate-limit accounting."""

import pytest

from app.petition import ValidationError, clean_text, valid_email, validate

VALID = {"name": "Ada Lovelace", "email": "ada@example.org", "location": "London", "consent": True}


@pytest.mark.parametrize(
    "email",
    ["a@b..co", "a..b@example.com", ".a@example.com", "a.@example.com", "a@-x.co", "a@x-.co", "a@x.-y.co",
     "a@example.c", "a@example.c0m", "<script>@x.co", '"a b"@x.co', "zoë@example.com", "a@exämple.com",
     "du​p@example.com", "ｄｕｐ@example.com", "nl@example.com\nBcc: x@y.co", "a@b@example.com",
     "a@localhost", "x" * 65 + "@example.com", "a@" + "b" * 64 + ".com", "a@.example.com", "a@example..com"],
)
def test_invalid_emails_rejected(email):
    assert not valid_email(email)


@pytest.mark.parametrize(
    "email",
    ["ada@example.org", "first.last+tag@sub.example.co.uk", "o'brien@example.ie", "a-b_c@my-host.example",
     "x@xn--bcher-kva.example", "user=1@example.com", "1@2.io"],
)
def test_valid_emails_accepted(email):
    assert valid_email(email)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Zoë ‮evil", "Zoë evil"),  # bidi override removed
        ("a​b", "ab"),  # zero-width space removed
        ("⁦Ada⁩", "Ada"),  # bidi isolates removed
        ("﻿Ada", "Ada"),  # BOM removed
        ("a\x00b\r\nc", "a b c"),  # controls become spaces
        ("  Ada \t Lovelace  ", "Ada Lovelace"),
        ("\U0001f468‍\U0001f469‍\U0001f467", "\U0001f468‍\U0001f469‍\U0001f467"),  # ZWJ emoji kept
        ("‍Ada‌", "Ada"),  # stray joiners at the ends removed
    ],
)
def test_clean_text(raw, clean):
    assert clean_text(raw) == clean


@pytest.mark.parametrize("name", ["​​", "‮", "‍", " \x00 ", "⁦⁩", "...", "﻿"])
def test_names_empty_after_cleaning_rejected(name):
    with pytest.raises(ValidationError):
        validate({**VALID, "name": name})


def test_name_and_location_stored_clean(client):
    from tests.conftest import ADMIN

    res = client.post("/api/petition", json={**VALID, "name": "Zoë ‮evil​", "location": "⁧Paris\x07"})
    assert res.status_code == 201
    csv_text = client.get("/api/petition/export.csv", headers={"Authorization": f"Bearer {ADMIN}"}).text
    assert "Zoë evil,ada@example.org,Paris," in csv_text


def test_invalid_email_rejected_by_api(client):
    assert client.post("/api/petition", json={**VALID, "email": "a@b..co"}).status_code == 422


def test_failed_attempts_do_not_use_up_signature_quota(make_client):
    client = make_client(sign_rate=2, sign_attempt_rate=20)
    for _ in range(5):  # typos / validation failures
        assert client.post("/api/petition", json={**VALID, "email": "bad@@example.org"}).status_code == 422
    assert client.post("/api/petition", json={**VALID, "email": "a@example.org"}).status_code == 201
    assert client.post("/api/petition", json={**VALID, "email": "a@example.org"}).status_code == 409  # duplicate
    assert client.post("/api/petition", json={**VALID, "email": "b@example.org"}).status_code == 201
    assert client.post("/api/petition", json={**VALID, "email": "c@example.org"}).status_code == 429


def test_attempts_have_their_own_higher_limit(make_client):
    client = make_client(sign_rate=2, sign_attempt_rate=4)
    codes = [client.post("/api/petition", json={**VALID, "email": "bad"}).status_code for _ in range(6)]
    assert codes == [422, 422, 422, 422, 429, 429]


def test_honeypot_counts_as_signature(make_client):
    client = make_client(sign_rate=1)
    assert client.post("/api/petition", json={**VALID, "website": "x"}).status_code == 201
    assert client.post("/api/petition", json=VALID).status_code == 429
