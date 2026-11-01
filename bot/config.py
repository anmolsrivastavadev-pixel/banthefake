from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """A safe configuration error that may be shown at startup."""


def validate_server_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ConfigError("BANTHEFAKE_URL is required.")
    if any(character in raw for character in " \t\r\n<>\"'\\"):
        raise ConfigError("BANTHEFAKE_URL contains an unsafe character.")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("BANTHEFAKE_URL is not a valid URL.") from exc

    if parsed.username or parsed.password:
        raise ConfigError("BANTHEFAKE_URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ConfigError("BANTHEFAKE_URL must not contain a query or fragment.")
    if not parsed.hostname:
        raise ConfigError("BANTHEFAKE_URL must contain a host name.")
    if parsed.scheme == "http":
        if parsed.hostname != "localhost":
            raise ConfigError("BANTHEFAKE_URL must use HTTPS; HTTP is allowed only for localhost development.")
    elif parsed.scheme != "https":
        raise ConfigError("BANTHEFAKE_URL must use HTTPS.")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _positive_float(name: str, value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number.") from exc
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero.")
    return number


def _optional_guild_id(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        guild_id = int(value)
    except ValueError as exc:
        raise ConfigError("BTF_DISCORD_GUILD_ID must be a Discord numeric ID.") from exc
    if guild_id <= 0:
        raise ConfigError("BTF_DISCORD_GUILD_ID must be a positive Discord numeric ID.")
    return guild_id


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    server_url: str
    max_upload_mb: float = 50.0
    public_results: bool = False
    guild_id: int | None = None

    def __post_init__(self) -> None:
        if not self.token:
            raise ConfigError("DISCORD_TOKEN is required.")
        object.__setattr__(self, "server_url", validate_server_url(self.server_url))
        if self.max_upload_mb <= 0:
            raise ConfigError("BTF_MAX_UPLOAD_MB must be greater than zero.")

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def ephemeral(self) -> bool:
        return not self.public_results

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("DISCORD_TOKEN", "")
        server_url = os.environ.get("BANTHEFAKE_URL", "")
        public_value = os.environ.get("BTF_DISCORD_PUBLIC_RESULTS", "off").strip().lower()
        if public_value not in {"off", "on"}:
            raise ConfigError("BTF_DISCORD_PUBLIC_RESULTS must be 'off' or 'on'.")
        return cls(
            token=token,
            server_url=server_url,
            max_upload_mb=_positive_float("BTF_MAX_UPLOAD_MB", os.environ.get("BTF_MAX_UPLOAD_MB", "50")),
            public_results=public_value == "on",
            guild_id=_optional_guild_id(os.environ.get("BTF_DISCORD_GUILD_ID")),
        )
