import json
import secrets
import os
import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, session, abort
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from werkzeug.middleware.proxy_fix import ProxyFix

from cw_auth import COOKIE_NAME, SESSION_SECONDS, TOKEN_DB, create_session, session_email, revoke_session

# ---- Config (via env vars) -------------------------------------------------
APP_DIR = Path(__file__).resolve().parent

# Public HTTPS base. REQUIRED.
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
REDIRECT_URI = f"{PUBLIC_BASE}/oauth2callback"

# OAuth client secrets JSON downloaded from Google Cloud Console
CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS", str(APP_DIR / "credentials.json"))

# SQLite DB path to store tokens
DB_PATH = TOKEN_DB

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
app.config.update(SESSION_COOKIE_NAME="__Host-cw_oauth", SESSION_COOKIE_SECURE=True,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_PATH="/")


@app.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

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


def get_user_email(creds: Credentials) -> str:
    """Return the connected Gmail account email."""
    svc = build("oauth2", "v2", credentials=creds)
    info = svc.userinfo().get().execute()
    email = (info.get("email") or "").strip().lower()
    if info.get("verified_email") is not True or "@" not in email:
        raise ValueError("Google did not return a verified email")
    return email


@app.get("/auth/start")
def auth_start():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        code_verifier=secrets.token_urlsafe(64),
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # ensures refresh_token is returned when needed
    )

    session.clear()
    session["oauth_state"] = state
    session["oauth_started_at"] = time.time()
    session["oauth_code_verifier"] = flow.code_verifier

    return redirect(auth_url)


@app.get("/oauth2callback")
def oauth2callback():
    expected = session.pop("oauth_state", None)
    started = session.pop("oauth_started_at", 0)
    code_verifier = session.pop("oauth_code_verifier", None)
    supplied = request.args.get("state", "")
    if (not expected or not supplied or not code_verifier
            or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
            or time.time() - started > 600):
        abort(400, "Invalid or expired sign-in. Please start again.")
    if request.args.get("error") or not request.args.get("code"):
        abort(400, "Google authorization was not completed. Please start again.")

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI, state=expected,
        code_verifier=code_verifier,
    )
    authorization_response = f"{PUBLIC_BASE}{request.full_path}".rstrip("?")
    try:
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials
        email = get_user_email(creds)
    except Exception:
        # Do not expose OAuth codes, credentials, or provider response details.
        abort(400, "Google sign-in failed. Please start again.")

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

    revoke_session(request.cookies.get(COOKIE_NAME), DB_PATH)
    login_secret = create_session(email, DB_PATH)
    session.clear()
    response = redirect(f"{PUBLIC_BASE}/complaint_warrior/")
    response.set_cookie(COOKIE_NAME, login_secret, max_age=SESSION_SECONDS,
                        secure=True, httponly=True, samesite="Lax", path="/")
    return response


@app.get("/auth/status")
def auth_status():
    email = session_email(request.cookies.get(COOKIE_NAME), DB_PATH)
    if not email:
        return jsonify({"connected": False}), 401
    return jsonify({"connected": load_token(email) is not None, "email": email})


if __name__ == "__main__":
    # localhost only; nginx exposes it
    app.run(host="127.0.0.1", port=8510, debug=False)
