"""Authenticated pointer to the active complaint; payload is read fresh on each GET."""
import json
import hashlib
import sqlite3
import time
from pathlib import Path
from cw_auth import TOKEN_DB, session_email


def _record(db_path, email, complaint_id):
    path = Path(db_path).resolve()
    # Read-only: a typo cannot create an empty database.
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT complaint_json FROM complaints WHERE lower(trim(user_email))=? AND complaint_id=?',
                         (email, str(complaint_id))).fetchone()
    if not row:
        raise LookupError('The selected complaint is unavailable for this signed-in user.')
    raw = json.loads(row[0])
    if not isinstance(raw, dict):
        raise ValueError('Invalid stored complaint.')
    return raw


def activate(secret, complaint_id, source_path, token_db=TOKEN_DB):
    email = session_email(secret, token_db)
    if not email:
        raise PermissionError('Sign in to CW again.')
    _record(source_path, email, complaint_id)  # SQL owner check, never JSON owner claims.
    with sqlite3.connect(token_db) as db:
        db.execute('CREATE TABLE IF NOT EXISTS cw_extension_selection ('
                   'session_digest TEXT PRIMARY KEY, user_email TEXT NOT NULL, complaint_id TEXT NOT NULL, source_path TEXT NOT NULL, updated_at REAL NOT NULL)')
        db.execute('INSERT INTO cw_extension_selection VALUES (?, ?, ?, ?, ?) '
                   'ON CONFLICT(session_digest) DO UPDATE SET complaint_id=excluded.complaint_id, '
                   'source_path=excluded.source_path, updated_at=excluded.updated_at',
                   (hashlib.sha256(secret.encode()).hexdigest(), email, str(complaint_id), str(Path(source_path).resolve()), time.time()))
    return email


def selected_payload(secret, token_db=TOKEN_DB):
    email = session_email(secret, token_db)
    if not email:
        raise PermissionError('Sign in to CW again.')
    with sqlite3.connect(token_db) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cw_extension_selection'").fetchone()
        row = db.execute('SELECT complaint_id, source_path, updated_at FROM cw_extension_selection WHERE user_email=? AND session_digest=?',
                         (email, hashlib.sha256(secret.encode()).hexdigest())).fetchone() if exists else None
    if not row or time.time() - row[2] > 12 * 60 * 60:
        raise LookupError('Open and select your complaint in CW first.')
    raw = _record(row[1], email, row[0])
    from cw_export import build_export, from_native
    # Repeat login check after the database read; do not cache payloads across users.
    if session_email(secret, token_db) != email:
        raise PermissionError('Sign in to CW again.')
    return json.loads(build_export(row[0], from_native(raw, email)))


def register_routes(app, cookie_name, token_db=TOKEN_DB):
    from flask import request, jsonify

    @app.after_request
    def private_extension_response(response):
        if request.path == '/auth/extension/complaint':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/auth/extension/complaint')
    def extension_complaint():
        # A custom header prevents ordinary cross-origin HTML requests. No permissive
        # CORS response is provided; the extension uses its explicit host permission.
        if request.headers.get('X-CW-Extension') != '1':
            return jsonify({'error':'Use the CW extension.'}), 403
        try:
            return jsonify(selected_payload(request.cookies.get(cookie_name), token_db))
        except PermissionError:
            return jsonify({'error':'CW login expired or unavailable. Sign in again.'}), 401
        except LookupError:
            return jsonify({'error':'Select a complaint in CW, then click the extension again.'}), 404
        except (sqlite3.Error, OSError, ValueError, ImportError):
            return jsonify({'error':'Selected complaint cannot be read. Check the CW server configuration.'}), 503
