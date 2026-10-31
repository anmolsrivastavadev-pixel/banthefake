import csv
import io
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")
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


def validate(payload: Any) -> Signature:
    if not isinstance(payload, dict):
        raise ValidationError("Invalid request body.")
    name = payload.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 100:
        raise ValidationError("Please enter your name (up to 100 characters).")
    email = normalize_email(payload.get("email"))
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValidationError("Please enter a valid email address.")
    location = payload.get("location") or ""
    if not isinstance(location, str) or len(location.strip()) > 100:
        raise ValidationError("Location must be 100 characters or fewer.")
    if payload.get("consent") is not True:
        raise ValidationError("Please tick the consent box to sign.")
    return Signature(name=name, email=email, location=location.strip())


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
