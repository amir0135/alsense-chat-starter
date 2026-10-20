import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.contracts import User

# Append-only. Databases created before versioning report user_version 0 and already have migration 1's tables.
MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, user_json TEXT, expires REAL
    );
    CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY, session_id TEXT, question TEXT, reply TEXT, created REAL
    );
    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, payload TEXT, result TEXT
    );
    CREATE TABLE IF NOT EXISTS unanswered (
        id INTEGER PRIMARY KEY, session_id TEXT, category TEXT, created REAL
    )
    """,
    """
    ALTER TABLE sessions ADD COLUMN faults TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE sessions ADD COLUMN scenario TEXT NOT NULL DEFAULT '';
    ALTER TABLE actions ADD COLUMN created REAL;
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY,
        created REAL NOT NULL,
        correlation_id TEXT NOT NULL,
        session_id TEXT,
        persona TEXT,
        scenario TEXT,
        event TEXT NOT NULL,
        flow_id TEXT,
        flow_version TEXT,
        tools TEXT NOT NULL,
        inputs TEXT NOT NULL,
        response_category TEXT,
        confirmation TEXT,
        outcome TEXT,
        error_category TEXT
    );
    CREATE INDEX audit_session ON audit (session_id, id)
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)
AUDIT_FIELDS = (
    "correlation_id",
    "session_id",
    "persona",
    "scenario",
    "event",
    "flow_id",
    "flow_version",
    "tools",
    "inputs",
    "response_category",
    "confirmation",
    "outcome",
    "error_category",
)


class MissingRecord(Exception):
    pass


class SchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionRecord:
    id: str
    user: User
    faults: frozenset[str]
    scenario: str


class Store:
    """Persistent local POC store. Not a distributed multi-replica database."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def migrate(self) -> None:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SchemaError(
                    f"The session database {self.path} uses schema v{version}, newer than this app supports "
                    f"(v{SCHEMA_VERSION}). Reset the demo data with: python scripts/dev.py reset"
                )
            for script in MIGRATIONS[version:]:
                for statement in script.split(";"):
                    if statement.strip():
                        db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def reset(path: str) -> list[str]:
        """Delete a demo database and its SQLite side files. Returns the files removed."""
        removed = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(path + suffix)
            if candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate))
        return removed

    def status(self) -> dict:
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sessions", "turns", "actions", "audit")
            }
        return {"schema_version": version, "current": version == SCHEMA_VERSION, "counts": counts}

    def create_session(
        self, user: User, ttl: int, faults: frozenset[str] = frozenset(), scenario: str = ""
    ) -> tuple[str, str]:
        token, session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions (id, token_hash, user_json, expires, faults, scenario) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    self.digest(token),
                    user.model_dump_json(),
                    time.time() + ttl,
                    json.dumps(sorted(faults)),
                    scenario,
                ),
            )
        return token, session_id

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token: str) -> SessionRecord:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND expires>?", (self.digest(token), time.time())
            ).fetchone()
        if row is None:
            raise MissingRecord("Session invalid or expired.")
        return SessionRecord(
            id=row["id"],
            user=User.model_validate_json(row["user_json"]),
            faults=frozenset(json.loads(row["faults"])),
            scenario=row["scenario"],
        )

    def add_turn(self, session_id: str, question: str, reply: dict) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO turns (session_id, question, reply, created) VALUES (?, ?, ?, ?)",
                (session_id, question, json.dumps(reply), time.time()),
            )

    def history(self, session_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT question, reply FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 20", (session_id,)
            ).fetchall()
        return [{"question": row["question"], "reply": json.loads(row["reply"])} for row in reversed(rows)]

    def record_unanswered(self, session_id: str, category: str) -> None:
        # Categories only: no automatic telemetry of user text or tokens.
        with self.connection() as db:
            db.execute(
                "INSERT INTO unanswered (session_id, category, created) VALUES (?, ?, ?)",
                (session_id, category, time.time()),
            )

    def audit(self, **record: Any) -> None:
        """Structured interaction record. Callers pass validated, allowlisted values only: no tokens or free text."""
        unknown = set(record) - set(AUDIT_FIELDS)
        if unknown:
            raise ValueError(f"Unknown audit fields: {sorted(unknown)}")
        values = {name: record.get(name) for name in AUDIT_FIELDS}
        values["tools"] = json.dumps(values["tools"] or [])
        values["inputs"] = json.dumps(values["inputs"] or [])
        with self.connection() as db:
            db.execute(
                f"INSERT INTO audit (created, {', '.join(AUDIT_FIELDS)}) "
                f"VALUES (?, {', '.join('?' for _ in AUDIT_FIELDS)})",
                (time.time(), *values.values()),
            )

    def audit_trail(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM audit" + (" WHERE session_id=?" if session_id else "") + " ORDER BY id DESC LIMIT ?"
        with self.connection() as db:
            rows = db.execute(query, (session_id, limit) if session_id else (limit,)).fetchall()
        records = []
        for row in reversed(rows):
            record = dict(row)
            record["tools"] = json.loads(record["tools"])
            record["inputs"] = json.loads(record["inputs"])
            records.append(record)
        return records

    def prepare(self, session_id: str, kind: str, payload: dict) -> dict:
        action_id = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute(
                "INSERT INTO actions (id, session_id, kind, payload, result, created) VALUES (?, ?, ?, ?, NULL, ?)",
                (action_id, session_id, kind, json.dumps(payload), time.time()),
            )
        return {"id": action_id, "kind": kind, "payload": payload, "state": "prepared"}

    def action(self, session_id: str, action_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
        if row is None:
            raise MissingRecord("Action unavailable.")
        return self.decode_action(row)

    @staticmethod
    def decode_action(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "state": "submitted" if row["result"] else "prepared",
        }

    def submit(self, session_id: str, action_id: str, execute: Callable[[dict], dict[str, Any]]) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
            if row is None:
                raise MissingRecord("Action unavailable.")
            action = self.decode_action(row)
            if action["result"] is not None:
                return action["result"]
            result = execute(action)
            db.execute("UPDATE actions SET result=? WHERE id=?", (json.dumps(result), action_id))
            return result
