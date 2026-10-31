import csv
import io
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Unquoted local part (RFC 5322 dot-atom): no leading, trailing or consecutive dots.
EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
# Domain label: letters, digits and hyphens, 1-63 chars, not starting or ending with a hyphen.
EMAIL_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
EMAIL_TLD_RE = re.compile(r"^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59})$")
# Invisible format characters that are still allowed inside names (needed by some scripts and emoji).
JOINERS = "\u200c\u200d"
CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Signature:
    name: str
    email: str
    location: str


def normalize_email(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def valid_email(email: str) -> bool:
    """ASCII-only, unquoted local part without stray dots, hostname-style domain with a real TLD."""
    if len(email) > 254 or not email.isascii():
        return False
    local, at, domain = email.rpartition("@")
    if not at or not 1 <= len(local) <= 64 or not EMAIL_LOCAL_RE.match(local):
        return False
    labels = domain.split(".")
    return (
        len(labels) >= 2
        and all(EMAIL_LABEL_RE.match(label) for label in labels)
        and bool(EMAIL_TLD_RE.match(labels[-1]))
    )


def clean_text(value: str) -> str:
    """Normalize free text: NFC, control characters become spaces, invisible format characters
    (zero-width spaces, bidi overrides/isolates, BOM...) are removed, whitespace is collapsed."""
    out = []
    for ch in unicodedata.normalize("NFC", value):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Zl", "Zp"):
            out.append(" ")
        elif (cat == "Cf" and ch not in JOINERS) or cat in ("Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split()).strip(JOINERS + " ")


def _visible(value: str) -> bool:
    return any(unicodedata.category(ch)[0] in "LNS" for ch in value)


def validate(payload: Any) -> Signature:
    if not isinstance(payload, dict):
        raise ValidationError("Invalid request body.")
    name = payload.get("name")
    name = clean_text(name) if isinstance(name, str) else ""
    if not 1 <= len(name) <= 100 or not _visible(name):
        raise ValidationError("Please enter your name (up to 100 characters).")
    email = normalize_email(payload.get("email"))
    if not valid_email(email):
        raise ValidationError("Please enter a valid email address.")
    location = payload.get("location") or ""
    if not isinstance(location, str):
        raise ValidationError("Location must be 100 characters or fewer.")
    location = clean_text(location)
    if len(location) > 100:
        raise ValidationError("Location must be 100 characters or fewer.")
    if payload.get("consent") is not True:
        raise ValidationError("Please tick the consent box to sign.")
    return Signature(name=name, email=email, location=location)


def csv_safe(value: str) -> str:
    return "'" + value if value.startswith(CSV_INJECTION_PREFIXES) else value


class PetitionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS signatures (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    location TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def add(self, sig: Signature) -> bool:
        """Store a signature; return False if the email already signed."""
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO signatures (name, email, location, created_at) VALUES (?, ?, ?, ?)",
                    (sig.name, sig.email, sig.location, created_at),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM signatures").fetchone()[0]

    def delete(self, email: str) -> bool:
        with closing(self._connect()) as db, db:
            cur = db.execute("DELETE FROM signatures WHERE email = ?", (normalize_email(email),))
            return cur.rowcount > 0

    def export_csv(self) -> str:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT name, email, location, created_at FROM signatures ORDER BY id"
            ).fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["name", "email", "location", "created_at"])
        for row in rows:
            writer.writerow([csv_safe(str(cell)) for cell in row])
        return out.getvalue()
