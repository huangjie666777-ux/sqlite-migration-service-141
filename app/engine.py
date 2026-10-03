"""Migration engine: validation, protected execution, version storage."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import MIGRATION_TABLE
from .sqlsplit import first_keyword, split_statements

# Statement types a migration script may start with. Anything else
# (SELECT, PRAGMA, ATTACH, DETACH, BEGIN, COMMIT, VACUUM, ...) is refused.
ALLOWED_LEADING_KEYWORDS = {
    "CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "DELETE", "REPLACE",
}

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    sha256      TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
)
"""


class MigrationError(Exception):
    """Expected, client-facing failure."""

    def __init__(self, message: str, *, version: int | None = None,
                 status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.version = version
        self.status_code = status_code


class BusyError(MigrationError):
    def __init__(self, message: str = "database is busy, retry later"):
        super().__init__(message, status_code=409)


@dataclass
class ManifestItem:
    version: int
    description: str
    sql: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(alias: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(alias, threading.Lock())


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(CREATE_TABLE_SQL)
    return conn


def _make_authorizer(state: dict):
    """Runtime guard: enforces policy regardless of SQL text tricks,
    including actions fired indirectly by triggers."""
    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
                      sqlite3.SQLITE_DELETE):
            if arg1 == MIGRATION_TABLE and not state.get("allow_meta_write"):
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_DROP_TABLE and arg1 == MIGRATION_TABLE:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == MIGRATION_TABLE:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorizer


def read_applied(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"SELECT version, description, sha256, applied_at "
        f"FROM {MIGRATION_TABLE} ORDER BY version"
    ).fetchall()
    return [
        {"version": v, "description": d, "sha256": s, "applied_at": t}
        for v, d, s, t in rows
    ]


def get_status(db_path: Path) -> dict:
    conn = _connect(db_path)
    try:
        applied = read_applied(conn)
        return {
            "current_version": applied[-1]["version"] if applied else 0,
            "applied": applied,
        }
    finally:
        conn.close()


def validate_manifest(items: list[ManifestItem]) -> None:
    if not items:
        raise MigrationError("manifest must contain at least one migration")
    expected = list(range(1, len(items) + 1))
    actual = [it.version for it in items]
    if len(set(actual)) != len(actual):
        raise MigrationError("duplicate versions in manifest")
    if actual != expected:
        raise MigrationError(
            "versions must be consecutive positive integers starting at 1")
    for it in items:
        if not it.description.strip():
            raise MigrationError("empty description", version=it.version)
        if not it.sql.strip():
            raise MigrationError("blank SQL script", version=it.version)
        stmts = split_statements(it.sql)
        if not stmts:
            raise MigrationError("script has no statements",
                                 version=it.version)
        for stmt in stmts:
            kw = first_keyword(stmt)
            if kw not in ALLOWED_LEADING_KEYWORDS:
                raise MigrationError(
                    f"statement type not allowed: {kw or stmt[:30]!r}",
                    version=it.version)


def _check_history(applied: list[dict], items: list[ManifestItem]) -> int:
    """Verify the manifest reproduces the applied history exactly.
    Returns the index of the first unapplied manifest item."""
    if len(applied) > len(items):
        raise MigrationError(
            "database has more migrations applied than the manifest contains",
            status_code=409)
    for idx, rec in enumerate(applied):
        item = items[idx]
        if item.version != rec["version"]:
            raise MigrationError(
                f"manifest version {item.version} does not match applied "
                f"version {rec['version']}", status_code=409)
        if item.digest != rec["sha256"]:
            raise MigrationError(
                f"script for version {item.version} was altered "
                f"(sha256 mismatch)", version=item.version, status_code=409)
    return len(applied)


def apply_migrations(db_path: Path, items: list[ManifestItem],
                     expected_version: int) -> dict:
    validate_manifest(items)
    if expected_version != items[-1].version:
        raise MigrationError(
            f"expected_version {expected_version} does not match the last "
            f"manifest version {items[-1].version}", status_code=409)

    lock = _lock_for(str(db_path))
    if not lock.acquire(blocking=False):
        raise BusyError()
    conn = None
    try:
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise BusyError(f"database is locked: {exc}") from exc
        try:
            applied = read_applied(conn)
            start = _check_history(applied, items)
            todo = items[start:]
            if not todo:
                conn.execute("COMMIT")
                return {
                    "applied_now": [],
                    "current_version": applied[-1]["version"] if applied else 0,
                    "message": "all migrations already applied",
                }
            state: dict = {}
            conn.set_authorizer(_make_authorizer(state))
            try:
                for item in todo:
                    try:
                        for stmt in split_statements(item.sql):
                            conn.execute(stmt)
                    except sqlite3.Error as exc:
                        raise MigrationError(
                            f"version {item.version} failed: {exc}",
                            version=item.version) from exc
                    state["allow_meta_write"] = True
                    try:
                        conn.execute(
                            f"INSERT INTO {MIGRATION_TABLE} "
                            f"(version, description, sha256, applied_at) "
                            f"VALUES (?, ?, ?, ?)",
                            (item.version, item.description, item.digest,
                             time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime())))
                    finally:
                        state["allow_meta_write"] = False
            finally:
                conn.set_authorizer(lambda *a: sqlite3.SQLITE_OK)
            fk_violations = conn.execute(
                "PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                raise MigrationError(
                    f"foreign key violations: {fk_violations[:5]}",
                    version=todo[-1].version)
            conn.execute("COMMIT")
            return {
                "applied_now": [it.version for it in todo],
                "current_version": todo[-1].version,
                "message": "migrations applied",
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        if conn is not None:
            conn.close()
        lock.release()
