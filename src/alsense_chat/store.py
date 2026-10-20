import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from .source import User


class MissingRecord(Exception):
    pass


class Store:
    """Persistent local POC store. Not a distributed multi-replica database."""

    def __init__(self, path: str):
        self.path = path
        with self.connection() as db:
            db.executescript("""
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
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_session(self, user: User, ttl: int) -> tuple[str, str]:
        token, session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, self.digest(token), user.model_dump_json(), time.time() + ttl),
            )
        return token, session_id

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token: str) -> tuple[str, User]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND expires>?", (self.digest(token), time.time())
            ).fetchone()
        if row is None:
            raise MissingRecord("Session invalid or expired.")
        return row["id"], User.model_validate_json(row["user_json"])

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

    def prepare(self, session_id: str, kind: str, payload: dict) -> dict:
        action_id = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, NULL)", (action_id, session_id, kind, json.dumps(payload))
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
