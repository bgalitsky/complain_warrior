"""Opaque, revocable login sessions shared by Flask and Streamlit.

Only the OAuth callback may issue a session after verifying Google's identity.
The browser receives a random secret; SQLite stores only its SHA-256 digest.
"""
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

COOKIE_NAME = "__Host-cw_session"
SESSION_SECONDS = 12 * 60 * 60
TOKEN_DB = os.environ.get(
    "GMAIL_TOKEN_DB", str(Path(__file__).resolve().parent / "cw_gmail_tokens.sqlite")
)


def _connect(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS cw_login_sessions (
        digest TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at REAL NOT NULL
    )""")
    return con


def _digest(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_session(email, db_path=TOKEN_DB):
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("A verified email is required")
    secret = secrets.token_urlsafe(32)
    con = _connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM cw_login_sessions WHERE expires_at <= ?", (time.time(),))
            con.execute("INSERT INTO cw_login_sessions VALUES (?, ?, ?)",
                        (_digest(secret), email, time.time() + SESSION_SECONDS))
    finally:
        con.close()
    return secret


def session_email(secret, db_path=TOKEN_DB):
    if not isinstance(secret, str) or not 32 <= len(secret) <= 128:
        return None
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT email FROM cw_login_sessions WHERE digest=? AND expires_at>?",
            (_digest(secret), time.time()),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def revoke_session(secret, db_path=TOKEN_DB):
    if not secret:
        return
    con = _connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM cw_login_sessions WHERE digest=?", (_digest(secret),))
    finally:
        con.close()
