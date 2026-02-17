import json
import sqlite3
import time
from typing import Optional, Dict, Any, List


class GmailTokenStore:
    """Very small SQLite token store.

    Keys are arbitrary strings.
    In multi-user mode we use key == user_email.
    """

    def __init__(self, db_path: str = "cw_gmail_tokens.sqlite"):
        self.db_path = db_path
        self._init()

    def _connect(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init(self):
        con = self._connect()
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_tokens(
              key TEXT PRIMARY KEY,
              token_json TEXT NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        con.commit()
        con.close()

    def set(self, key: str, token_json: Dict[str, Any]):
        con = self._connect()
        con.execute(
            "INSERT OR REPLACE INTO gmail_tokens(key, token_json, updated_at) VALUES(?,?,?)",
            (key, json.dumps(token_json), time.time()),
        )
        con.commit()
        con.close()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        con = self._connect()
        row = con.execute("SELECT token_json FROM gmail_tokens WHERE key=?", (key,)).fetchone()
        con.close()
        return json.loads(row[0]) if row else None

    def list_keys(self) -> List[str]:
        con = self._connect()
        rows = con.execute("SELECT key FROM gmail_tokens ORDER BY updated_at DESC").fetchall()
        con.close()
        return [r[0] for r in rows]

    def delete(self, key: str) -> None:
        con = self._connect()
        con.execute("DELETE FROM gmail_tokens WHERE key=?", (key,))
        con.commit()
        con.close()
