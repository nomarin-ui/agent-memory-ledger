"""Schema versioning. Migrations are applied in order, exactly once, ever."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d{3})_(\w+)\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
"""


class MigrationError(Exception):
    """A migration is missing, misnumbered, or failed to apply."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Find all migration files, ordered by version.

    >>> discover()[0].version
    1
    """
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"bad migration filename: {path.name} (want NNN_name.sql)")
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=path.read_text(encoding="utf-8"),
            )
        )

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 1, got {versions}")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions already applied to this database."""
    conn.executescript(BOOTSTRAP)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied version. 0 means an empty database."""
    applied = applied_versions(conn)
    return max(applied) if applied else 0


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    Needed because executescript() implicitly commits, which would break
    the atomic transaction we wrap each migration in. Naive on semicolons:
    do not put one inside a string literal or a trigger body.
    """
    return [s.strip() for s in sql.split(";") if s.strip()]


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every unapplied migration, in order. Returns versions applied.

    Each migration commits atomically -- a failure mid-way leaves the
    database at the last good version, never half-migrated.

    >>> migrate(conn)
    [1]
    """
    pending = [m for m in discover(directory) if m.version not in applied_versions(conn)]
    done: list[int] = []

    for m in pending:
        conn.execute("BEGIN")
        try:
            for statement in _split_statements(m.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (m.version, m.name, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise MigrationError(f"migration {m.version:03d}_{m.name} failed: {exc}") from exc
        done.append(m.version)

    return done
