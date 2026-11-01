import logging

import pytest

from bot.app import default_intents
from bot.config import Config, ConfigError, validate_server_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://scan.example.org/", "https://scan.example.org"),
        ("https://scan.example.org/base/", "https://scan.example.org/base"),
        ("http://localhost:8000/", "http://localhost:8000"),
    ],
)
def test_validate_server_url(value, expected):
    assert validate_server_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://example.org",
        "http://127.0.0.1:8000",
        "ftp://example.org",
        "https://user:secret@example.org",
        "https://example.org/?token=secret",
        "https://example.org/#fragment",
        "",
    ],
)
def test_validate_server_url_rejects_unsafe_values(value):
    with pytest.raises(ConfigError):
        validate_server_url(value)


def test_default_intents_request_no_privileged_intents():
    intents = default_intents()
    assert not intents.message_content
    assert not intents.members
    assert not intents.presences


def test_token_is_not_in_config_repr_or_logs(caplog):
    token = "super-secret-discord-token"
    config = Config(token=token, server_url="https://scan.example.org")
    with caplog.at_level(logging.INFO):
        logging.getLogger("banthefake.discord").info("configuration loaded: %r", config)
    assert token not in repr(config)
    assert token not in caplog.text
