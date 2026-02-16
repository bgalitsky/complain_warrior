import json
import os
import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, session
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from werkzeug.middleware.proxy_fix import ProxyFix

# ---- Config (via env vars) -------------------------------------------------
APP_DIR = Path(__file__).resolve().parent

# Public HTTPS base (ngrok). REQUIRED.
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
REDIRECT_URI = f"{PUBLIC_BASE}/oauth2callback"

# OAuth client secrets JSON downloaded from Google Cloud Console
CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS", str(APP_DIR / "credentials.json"))

# SQLite DB path to store tokens
DB_PATH = os.environ.get("GMAIL_TOKEN_DB", str(APP_DIR / "cw_gmail_tokens.sqlite"))

# If set to "1", also write a single-user convenience key named "default".
WRITE_DEFAULT_TOKEN = os.environ.get("WRITE_DEFAULT_TOKEN", "0") == "1"

# Scopes must match what you request and what Google returns.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# ---- App -------------------------------------------------------------------
app = Flask(__name__)

# Needed to store the oauth state in a signed cookie.
# Set a stable value in EC2: export FLASK_SECRET_KEY='...'
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

# Trust proxy headers (nginx). This helps, but we *also* force https in the
# authorization_response below because ngrok terminates TLS.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
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
    return con


def save_token(key: str, token_json: dict) -> None:
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO gmail_tokens(key, token_json, updated_at) VALUES(?,?,?)",
        (key, json.dumps(token_json), time.time()),
    )
    con.commit()
    con.close()


from typing import Optional, Dict

def load_token(key: str) -> Optional[Dict]:
    con = _db()
    row = con.execute("SELECT token_json FROM gmail_tokens WHERE key=?", (key,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def list_keys() -> list[str]:
    con = _db()
    rows = con.execute("SELECT key FROM gmail_tokens ORDER BY updated_at DESC").fetchall()
    con.close()
    return [r[0] for r in rows]


def get_user_email(creds: Credentials) -> str:
    """Return the connected Gmail account email."""
    svc = build("oauth2", "v2", credentials=creds)
    info = svc.userinfo().get().execute()
    return info.get("email", "") or "unknown"


@app.get("/auth/start")
def auth_start():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # ensures refresh_token is returned when needed
    )

    # Keep state for CSRF protection (best-effort; requires cookies)
    session["oauth_state"] = state

    return redirect(auth_url)


@app.get("/oauth2callback")
def oauth2callback():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    # Restore expected state if present.
    if session.get("oauth_state"):
        flow.state = session.get("oauth_state")

    # IMPORTANT:
    # ngrok terminates TLS and forwards to nginx over HTTP, so Flask will see
    # http://... unless we force https for the authorization_response.
    authorization_response = f"{PUBLIC_BASE}{request.full_path}".rstrip("?")

    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials

    email = get_user_email(creds)

    token_json = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
    }

    # Multi-user: store under the actual Gmail address.
    save_token(email, token_json)

    # Optional single-user convenience key.
    if WRITE_DEFAULT_TOKEN:
        save_token("default", token_json)

    return (
        f"Gmail connected for {email}. You can close this tab and return to the app."
    )


@app.get("/auth/status")
def auth_status():
    keys = list_keys()
    return jsonify(
        {
            "connected": len(keys) > 0,
            "accounts": keys,
            "db_path": DB_PATH,
            "write_default": WRITE_DEFAULT_TOKEN,
        }
    )


if __name__ == "__main__":
    # localhost only; nginx exposes it
    app.run(host="127.0.0.1", port=8510, debug=False)
