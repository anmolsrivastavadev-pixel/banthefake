import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("banthefake")

MIN_ADMIN_TOKEN_LEN = 24

ENVIRONMENTS = ("development", "production")
PRIVACY_CONTACT_PLACEHOLDER = "privacy@YOUR-DOMAIN.example"
# Deliberately conservative: no quotes, angle brackets, spaces or control characters can get through, so the
# value is safe in an href even before it is HTML-escaped (which it is anyway).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_URL_CHARS = r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]"
_HTTPS_URL_RE = re.compile(
    r"https://[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+(?::[0-9]{1,5})?"
    rf"(?:/{_URL_CHARS}*)?(?:\?{_URL_CHARS}*)?(?:#{_URL_CHARS}*)?"
)
# RFC 2606 / RFC 6761 names that can never be a real, monitored contact.
_RESERVED_DOMAIN_RE = re.compile(r"(?:^|\.)(?:example(?:\.(?:com|org|net))?|invalid|test|localhost)$", re.I)


class ConfigError(Exception):
    """A setting is invalid: the app must not start."""


def validate_privacy_contact(raw: str) -> str:
    """Return the trimmed contact (an email address or an https:// URL), or raise ConfigError."""
    value = raw.strip()
    if len(value) > 254 or not (_EMAIL_RE.fullmatch(value) or _HTTPS_URL_RE.fullmatch(value)):
        raise ConfigError(
            "BTF_PRIVACY_CONTACT must be an email address (privacy@your-domain.org) or an https:// URL, "
            "without spaces, quotes or angle brackets."
        )
    return value


def is_url_contact(contact: str) -> bool:
    return contact.startswith("https://")


def _contact_domain(contact: str) -> str:
    if is_url_contact(contact):
        host = contact[len("https://"):]
        for sep in "/?#":
            host = host.split(sep, 1)[0]
        return host.rsplit(":", 1)[0]
    return contact.rsplit("@", 1)[1]


def is_placeholder_contact(contact: str | None) -> bool:
    """True when no real contact is configured: unset, the shipped placeholder, or a reserved example domain."""
    if not contact:
        return True
    return bool(_RESERVED_DOMAIN_RE.search(_contact_domain(contact).rstrip(".")))


def _admin_token() -> str | None:
    """BTF_ADMIN_TOKEN, or None (admin endpoints disabled, 404) if it is unset or too short to resist guessing."""
    token = (os.environ.get("BTF_ADMIN_TOKEN") or "").strip()
    if not token:
        return None
    if len(token) < MIN_ADMIN_TOKEN_LEN:
        log.error(
            "BTF_ADMIN_TOKEN is shorter than %d characters: admin endpoints are DISABLED. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'",
            MIN_ADMIN_TOKEN_LEN,
        )
        return None
    return token


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"on", "1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/banthefake.db")
    max_upload_mb: int = 50
    upload_timeout_s: float = 60.0  # app fallback; production proxies should enforce a tighter body timeout
    scan_rate: int = 10
    scan_concurrency: int = 2  # scans decoding media at the same time (memory bound)
    scan_wait_s: float = 30.0  # how long a scan may queue for a slot before 503 "busy"
    sign_rate: int = 5  # successful signatures per hour per IP
    sign_attempt_rate: int = 30  # all petition POSTs (including rejected ones) per hour per IP
    petition_goal: int = 10000
    admin_token: str | None = None
    admin_fail_rate: int = 10  # failed admin authentications per hour per IP
    client_ip_header: str | None = None  # operator-trusted header containing one IP address
    hsts: bool = False
    model: str = "auto"
    env: str = "development"  # BTF_ENV; "production" refuses to start without a real privacy contact
    privacy_contact: str | None = None  # BTF_PRIVACY_CONTACT; validated by check()
    static_dir: Path = Path(__file__).resolve().parent.parent / "static"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def production(self) -> bool:
        return self.env == "production"

    def check(self) -> None:
        """Raise ConfigError for settings the app must not start with."""
        if self.env not in ENVIRONMENTS:
            raise ConfigError(f"BTF_ENV must be one of: {', '.join(ENVIRONMENTS)} (got {self.env!r}).")
        if self.privacy_contact is not None:
            validate_privacy_contact(self.privacy_contact)
        if self.production and is_placeholder_contact(self.privacy_contact):
            raise ConfigError(
                "BTF_ENV=production but BTF_PRIVACY_CONTACT is unset or a placeholder. Set it to a real, "
                "monitored email address or https:// URL for privacy questions and deletion requests."
            )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("BTF_DB_PATH") or cls.db_path),
            max_upload_mb=_int("BTF_MAX_UPLOAD_MB", cls.max_upload_mb),
            upload_timeout_s=_float("BTF_UPLOAD_TIMEOUT_S", cls.upload_timeout_s),
            scan_rate=_int("BTF_SCAN_RATE", cls.scan_rate),
            scan_concurrency=max(1, _int("BTF_SCAN_CONCURRENCY", cls.scan_concurrency)),
            scan_wait_s=_float("BTF_SCAN_WAIT_S", cls.scan_wait_s),
            sign_rate=_int("BTF_SIGN_RATE", cls.sign_rate),
            sign_attempt_rate=_int("BTF_SIGN_ATTEMPT_RATE", cls.sign_attempt_rate),
            petition_goal=_int("BTF_PETITION_GOAL", cls.petition_goal),
            admin_token=_admin_token(),
            admin_fail_rate=_int("BTF_ADMIN_FAIL_RATE", cls.admin_fail_rate),
            client_ip_header=(os.environ.get("BTF_CLIENT_IP_HEADER") or "").strip() or None,
            hsts=_enabled("BTF_HSTS"),
            model=(os.environ.get("BTF_MODEL") or cls.model).strip().lower(),
            env=(os.environ.get("BTF_ENV") or cls.env).strip().lower(),
            privacy_contact=(os.environ.get("BTF_PRIVACY_CONTACT") or "").strip() or None,
        )
