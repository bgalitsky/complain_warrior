"""storage.py

SQLite persistence for Complaint Warrior.

Design goals:
- Multi-user: every row is scoped by user_email.
- Simple: store the full ComplaintState JSON blob; the manager owns the schema.
- Safe defaults: create tables automatically.

This file intentionally has *no* external dependencies.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional


class ComplaintStore:
    """Stores ComplaintState JSON blobs keyed by (user_email, complaint_id)."""

    def __init__(self, db_path: str = "cw_store.sqlite"):
        self.db_path = db_path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False allows use from background threads
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init(self) -> None:
        con = self._connect()
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
              user_email     TEXT NOT NULL,
              complaint_id   TEXT NOT NULL,
              complaint_json TEXT NOT NULL,
              updated_at     REAL NOT NULL,
              PRIMARY KEY (user_email, complaint_id)
            )
            """
        )
        con.commit()
        con.close()

    def upsert(self, user_email: str, complaint_id: str, complaint_json: Dict[str, Any]) -> None:
        con = self._connect()
        con.execute(
            """
            INSERT OR REPLACE INTO complaints(user_email, complaint_id, complaint_json, updated_at)
            VALUES(?,?,?,?)
            """,
            (user_email, complaint_id, json.dumps(complaint_json, ensure_ascii=False), time.time()),
        )
        con.commit()
        con.close()

    def delete(self, user_email: str, complaint_id: str) -> None:
        con = self._connect()
        con.execute(
            "DELETE FROM complaints WHERE user_email=? AND complaint_id=?",
            (user_email, complaint_id),
        )
        con.commit()
        con.close()

    def get(self, user_email: str, complaint_id: str) -> Optional[Dict[str, Any]]:
        con = self._connect()
        row = con.execute(
            "SELECT complaint_json FROM complaints WHERE user_email=? AND complaint_id=?",
            (user_email, complaint_id),
        ).fetchone()
        con.close()
        return json.loads(row[0]) if row else None

    def load_all(self, user_email: str) -> Dict[str, Dict[str, Any]]:
        con = self._connect()
        rows = con.execute(
            "SELECT complaint_id, complaint_json FROM complaints WHERE user_email=?",
            (user_email,),
        ).fetchall()
        con.close()
        out: Dict[str, Dict[str, Any]] = {}
        for cid, js in rows:
            out[cid] = json.loads(js)
        return out

    def list_ids(self, user_email: str) -> List[str]:
        con = self._connect()
        rows = con.execute(
            "SELECT complaint_id FROM complaints WHERE user_email=? ORDER BY updated_at DESC",
            (user_email,),
        ).fetchall()
        con.close()
        return [r[0] for r in rows]


class CallResultStore:
    """Stores Twilio call results keyed by call_sid."""

    def __init__(self, db_path: str = "cw_store.sqlite"):
        self.db_path = db_path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init(self) -> None:
        con = self._connect()
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS call_results (
              call_sid     TEXT PRIMARY KEY,
              result_json  TEXT NOT NULL,
              updated_at   REAL NOT NULL
            )
            """
        )
        con.commit()
        con.close()

    def set(self, call_sid: str, result_json: Dict[str, Any]) -> None:
        con = self._connect()
        con.execute(
            "INSERT OR REPLACE INTO call_results(call_sid, result_json, updated_at) VALUES(?,?,?)",
            (call_sid, json.dumps(result_json, ensure_ascii=False), time.time()),
        )
        con.commit()
        con.close()

    def get(self, call_sid: str) -> Optional[Dict[str, Any]]:
        con = self._connect()
        row = con.execute(
            "SELECT result_json FROM call_results WHERE call_sid=?",
            (call_sid,),
        ).fetchone()
        con.close()
        return json.loads(row[0]) if row else None
