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
from .timing import valid_record

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
    """
    ALTER TABLE audit ADD COLUMN domains TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE audit ADD COLUMN offered_tools TEXT NOT NULL DEFAULT '[]'
    """,
    """
    ALTER TABLE audit ADD COLUMN assistant TEXT;
    ALTER TABLE audit ADD COLUMN timings TEXT NOT NULL DEFAULT '{}';
    CREATE UNIQUE INDEX audit_client_timing ON audit (session_id, correlation_id) WHERE event = 'client_timing'
    """,
    """
    ALTER TABLE turns ADD COLUMN answer_id TEXT;
    ALTER TABLE turns ADD COLUMN correlation_id TEXT;
    CREATE UNIQUE INDEX turns_answer ON turns (answer_id);
    ALTER TABLE audit ADD COLUMN answer_id TEXT;
    ALTER TABLE audit ADD COLUMN app_version TEXT;
    ALTER TABLE audit ADD COLUMN data_source TEXT;
    CREATE INDEX audit_answer ON audit (answer_id);
    CREATE TABLE feedback (
        answer_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        correlation_id TEXT,
        rating TEXT NOT NULL,
        reason TEXT,
        context TEXT NOT NULL,
        created REAL NOT NULL,
        updated REAL NOT NULL
    )
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
    "domains",
    "offered_tools",
    "response_category",
    "confirmation",
    "outcome",
    "error_category",
    "assistant",
    "timings",
    "answer_id",
    "app_version",
    "data_source",
)
# Written with every audit record unless the caller sets them.
STAMP_FIELDS = ("app_version", "data_source")


class MissingRecord(Exception):
    pass


class DuplicateRecord(Exception):
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

    def __init__(self, path: str, stamp: dict[str, str] | None = None):
        self.path = path
        # The app version and data source that serve this process, so each audit record says what produced it.
        self.stamp = {name: value for name, value in (stamp or {}).items() if name in STAMP_FIELDS}
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

    def add_turn(
        self, session_id: str, question: str, reply: dict, answer_id: str | None = None, correlation_id: str = ""
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO turns (session_id, question, reply, created, answer_id, correlation_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, question, json.dumps(reply), time.time(), answer_id, correlation_id or None),
            )

    def answer(self, session_id: str, answer_id: str) -> dict:
        """A stored reply of this session by its answer id, with the correlation id of the request that made it."""
        with self.connection() as db:
            row = db.execute(
                "SELECT reply, correlation_id FROM turns WHERE answer_id=? AND session_id=?", (answer_id, session_id)
            ).fetchone()
        if row is None:
            raise MissingRecord("Answer unavailable.")
        return {"reply": json.loads(row["reply"]), "correlation_id": row["correlation_id"]}

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
        for name, value in self.stamp.items():
            values[name] = values[name] or value
        for name in ("tools", "inputs", "domains", "offered_tools"):
            values[name] = json.dumps(values[name] or [])
        timings = values["timings"] or {}
        if not valid_record(timings):
            raise ValueError("Timings hold milliseconds and run counts for known stages only.")
        values["timings"] = json.dumps(timings, allow_nan=False)
        try:
            with self.connection() as db:
                db.execute(
                    f"INSERT INTO audit (created, {', '.join(AUDIT_FIELDS)}) "
                    f"VALUES (?, {', '.join('?' for _ in AUDIT_FIELDS)})",
                    (time.time(), *values.values()),
                )
        except sqlite3.IntegrityError as exc:
            # The only unique audit records are the widget's timings: one per request and session.
            if exc.sqlite_errorname != "SQLITE_CONSTRAINT_UNIQUE":
                raise
            raise DuplicateRecord("Already recorded.") from None

    def request_count(self, session_id: str, correlation_id: str) -> int:
        """How many chat turns or guided-check steps of one session carry this correlation id."""
        with self.connection() as db:
            return db.execute(
                "SELECT COUNT(*) FROM audit WHERE session_id=? AND correlation_id=? AND event IN ('chat', 'flow')",
                (session_id, correlation_id),
            ).fetchone()[0]

    def answer_record(self, session_id: str, answer_id: str) -> dict | None:
        """The audit record of the chat turn or guided-check step that produced an answer of this session."""
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM audit WHERE session_id=? AND answer_id=? AND event IN ('chat', 'flow') "
                "ORDER BY id LIMIT 1",
                (session_id, answer_id),
            ).fetchone()
        return self.decode_audit(row) if row else None

    def audit_trail(self, session_id: str | None = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM audit" + (" WHERE session_id=?" if session_id else "") + " ORDER BY id DESC LIMIT ?"
        with self.connection() as db:
            rows = db.execute(query, (session_id, limit) if session_id else (limit,)).fetchall()
        return [self.decode_audit(row) for row in reversed(rows)]

    @staticmethod
    def decode_audit(row: sqlite3.Row) -> dict:
        record = dict(row)
        for name in ("tools", "inputs", "domains", "offered_tools", "timings"):
            record[name] = json.loads(record[name])
        return record

    def save_feedback(
        self,
        answer_id: str,
        *,
        session_id: str,
        user_id: str,
        correlation_id: str | None,
        rating: str,
        reason: str | None,
        context: dict,
    ) -> dict:
        """One rating per answer: a later rating replaces it, and the context is captured again with it."""
        now = time.time()
        with self.connection() as db:
            db.execute(
                "INSERT INTO feedback (answer_id, session_id, user_id, correlation_id, rating, reason, context, "
                "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (answer_id) DO UPDATE SET rating=excluded.rating, reason=excluded.reason, "
                "context=excluded.context, updated=excluded.updated",
                (
                    answer_id,
                    session_id,
                    user_id,
                    correlation_id,
                    rating,
                    reason,
                    json.dumps(context, allow_nan=False),
                    now,
                    now,
                ),
            )
            row = db.execute("SELECT * FROM feedback WHERE answer_id=?", (answer_id,)).fetchone()
        return {**dict(row), "context": json.loads(row["context"])}

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
