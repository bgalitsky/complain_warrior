# storage.py
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB = os.environ.get("CW_DB_PATH", "cw_multiuser.sqlite")

def _connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con

def init_db(db_path: str = DEFAULT_DB) -> None:
    con = _connect(db_path)
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS complaints (
        user_email TEXT NOT NULL,
        complaint_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        complaint_json TEXT NOT NULL,
        PRIMARY KEY (user_email, complaint_id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS call_results (
        user_email TEXT NOT NULL,
        call_sid TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER,
        transcript TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_email, call_sid)
    );
    """)

    con.commit()
    con.close()

class ComplaintStore:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        init_db(db_path)

    def upsert_complaint(self, user_email: str, complaint_id: str, complaint: Dict[str, Any]) -> None:
        now = int(time.time())
        payload = json.dumps(complaint, ensure_ascii=False)
        con = _connect(self.db_path)
        con.execute(
            """
            INSERT INTO complaints(user_email, complaint_id, created_at, updated_at, complaint_json)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_email, complaint_id)
            DO UPDATE SET updated_at=excluded.updated_at, complaint_json=excluded.complaint_json
            """,
            (user_email, complaint_id, now, now, payload),
        )
        con.commit()
        con.close()

    def get_complaint(self, user_email: str, complaint_id: str) -> Optional[Dict[str, Any]]:
        con = _connect(self.db_path)
        row = con.execute(
            "SELECT complaint_json FROM complaints WHERE user_email=? AND complaint_id=?",
            (user_email, complaint_id),
        ).fetchone()
        con.close()
        if not row:
            return None
        return json.loads(row[0])

    def list_complaints(self, user_email: str, limit: int = 200) -> List[Tuple[str, int, int]]:
        con = _connect(self.db_path)
        rows = con.execute(
            """
            SELECT complaint_id, created_at, updated_at
            FROM complaints
            WHERE user_email=?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_email, limit),
        ).fetchall()
        con.close()
        return rows

    def delete_complaint(self, user_email: str, complaint_id: str) -> None:
        con = _connect(self.db_path)
        con.execute(
            "DELETE FROM complaints WHERE user_email=? AND complaint_id=?",
            (user_email, complaint_id),
        )
        con.commit()
        con.close()

class CallResultStore:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        init_db(db_path)

    def upsert_transcript(self, user_email: str, call_sid: str, transcript: str) -> None:
        now = int(time.time())
        con = _connect(self.db_path)
        con.execute(
            """
            INSERT INTO call_results(user_email, call_sid, created_at, updated_at, transcript)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_email, call_sid)
            DO UPDATE SET updated_at=excluded.updated_at, transcript=excluded.transcript
            """,
            (user_email, call_sid, now, now, transcript),
        )
        con.commit()
        con.close()

    def get_transcript(self, user_email: str, call_sid: str) -> Optional[Dict[str, Any]]:
        con = _connect(self.db_path)
        row = con.execute(
            """
            SELECT call_sid, transcript, updated_at
            FROM call_results
            WHERE user_email=? AND call_sid=?
            """,
            (user_email, call_sid),
        ).fetchone()
        con.close()
        if not row:
            return None
        return {"call_sid": row[0], "transcript": row[1], "updated": row[2]}
