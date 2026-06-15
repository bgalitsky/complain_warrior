#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Charge-back Initiator
---------------------
A Streamlit app that reads complaints from the Complaint Warrior SQLite database,
searches the user's Gmail and optionally Google Contacts for likely bank advisor/
manager recipients, drafts a charge-back request email, and optionally creates a
Gmail Draft or sends the email.

Updated to read OAuth tokens from a SQLite token store (cw_gmail_tokens.sqlite)
instead of token.json. Falls back to token.json / credentials.json only when a
SQLite token store is unavailable.
"""

from __future__ import annotations

import base64
import configparser
import datetime as dt
import email.utils
import json
import os
import re
import sqlite3
from dataclasses import dataclass, asdict


class TokenRefreshError(RuntimeError):
    """Raised when a stored OAuth token can no longer be refreshed."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import streamlit as st

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except Exception:
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None


APP_TITLE = "Charge-back Initiator"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.ini"


def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing configuration file: {CONFIG_PATH}")
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


CFG = _load_config()


def cfg_get(section: str, option: str, default: str = "") -> str:
    for existing_section in CFG.sections():
        if existing_section.lower() == section.lower():
            return CFG.get(existing_section, option, fallback=default).strip()
    return default


def cfg_path(section: str, option: str, default: str) -> Path:
    raw = cfg_get(section, option, default)
    path = Path(raw)
    return path if path.is_absolute() else APP_DIR / path


def cfg_int(section: str, option: str, default: int) -> int:
    try:
        return int(cfg_get(section, option, str(default)))
    except (TypeError, ValueError):
        return default


OPENAI_API_KEY = cfg_get("OpenAI", "api_key")
OPENAI_MODEL = cfg_get("OpenAI", "model", "gpt-4o-mini")
CONFIG_DB_PATH = cfg_path("Database", "path", "cw_store.sqlite")
CONFIG_TOKEN_DB_PATH = cfg_path("Gmail", "token_db", "cw_gmail_tokens.sqlite")
CONFIG_TOKEN_KEY = cfg_get("Gmail", "token_key", "default")
CONFIG_CREDENTIALS_PATH = cfg_path("Gmail", "credentials_file", "credentials.json")
CONFIG_TOKEN_PATH = cfg_path("Gmail", "token_file", "token.json")
SEARCH_KEYWORDS = [
    item.strip()
    for item in cfg_get(
        "ChargeBack",
        "search_keywords",
        "credit card,debit card,fraudulent,transaction",
    ).split(",")
    if item.strip()
]
MAX_GMAIL_SEARCH_RESULTS = cfg_int("ChargeBack", "max_search_results", 50)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
ALL_APP_SCOPES = GMAIL_SCOPES + [CONTACTS_SCOPE]

DEFAULT_DB_CANDIDATES = [
    "cw_store.sqlite",
    "cw_store(1).sqlite",
    "cw_store.db",
    "cw_store.sqlite3",
]
DEFAULT_TOKEN_DB_CANDIDATES = [
    "cw_gmail_tokens.sqlite",
    "cw_gmail_tokens.db",
    "gmail_tokens.sqlite",
    "gmail_tokens.db",
]

COMMON_BANK_DOMAINS = {
    "bank of america": "bankofamerica.com",
    "wells fargo": "wellsfargo.com",
    "chase": "chase.com",
    "jpmorgan": "jpmorgan.com",
    "citibank": "citi.com",
    "citi": "citi.com",
    "capital one": "capitalone.com",
    "us bank": "usbank.com",
    "u.s. bank": "usbank.com",
    "pnc": "pnc.com",
    "truist": "truist.com",
    "regions": "regions.com",
    "charles schwab": "schwab.com",
    "fidelity": "fidelity.com",
    "merrill": "merrilledge.com",
    "vanguard": "vanguard.com",
}

TRANSACTION_PATTERNS = [
    r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)",
    r"([0-9][0-9,]*(?:\.[0-9]{2})?)\s?(?:usd|dollars?)",
]
DATE_PATTERNS = [
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2},\s*\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
]


@dataclass
class ComplaintCase:
    complaint_id: str
    subject: str
    complaint_raw: str
    complaint_professional: str
    current_status_summary: str
    final_conclusion: str
    created_at: str
    user_email: str
    user_name: str
    docs: List[str]
    evidence_pack_pdf: Optional[str]
    activities: List[dict]
    threads: Dict[str, Any]


@dataclass
class GmailCandidate:
    source: str
    name: str
    email: str
    last_seen: str
    message_count: int
    score: float
    sample_subjects: List[str]


@dataclass
class GmailSearchMessage:
    message_id: str
    thread_id: str
    from_name: str
    from_email: str
    to_names: List[str]
    to_emails: List[str]
    cc_emails: List[str]
    subject: str
    date: str
    snippet: str
    body_excerpt: str


@dataclass
class BankerSelection:
    selected_message_id: str
    banker_name: str
    banker_email: str
    direction: str
    confidence: float
    reason: str


@dataclass
class BankerTransactionVerification:
    verified: bool
    confidence: float
    reason: str
    evidence: str



# -----------------------------
# DB helpers
# -----------------------------
def _discover_file(candidates: List[str], patterns: Tuple[str, ...]) -> Optional[Path]:
    cwd = Path.cwd()
    for name in candidates:
        candidate = cwd / name
        if candidate.exists():
            return candidate
    for ext in patterns:
        matches = sorted(cwd.glob(ext), key=lambda p: (candidates[0].split('.')[0] not in p.name.lower(), p.name.lower()))
        if matches:
            return matches[0]
    return None


def discover_token_db_path() -> Optional[Path]:
    search_roots: List[Path] = []
    cwd = Path.cwd()
    for candidate in [cwd, cwd.parent, Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent]:
        if candidate not in search_roots and candidate.exists():
            search_roots.append(candidate)

    ranked: List[Path] = []
    for root in search_roots:
        for name in DEFAULT_TOKEN_DB_CANDIDATES:
            p = root / name
            if p.exists() and p not in ranked:
                ranked.append(p)
        for pat in ("*gmail*token*.sqlite", "*gmail*token*.db", "*.sqlite", "*.db"):
            for p in sorted(root.glob(pat)):
                if p not in ranked:
                    ranked.append(p)

    valid = [p for p in ranked if token_store_ready(p)]
    return valid[0] if valid else None


def _sqlite_table_names(path: Path) -> List[str]:
    try:
        con = sqlite3.connect(str(path))
        try:
            rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def is_complaint_warrior_db(path: Path) -> bool:
    tables = set(_sqlite_table_names(path))
    return "complaints" in tables and "call_results" in tables


def discover_db_path() -> Optional[Path]:
    search_roots: List[Path] = []
    cwd = Path.cwd()
    for candidate in [cwd, cwd.parent, Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent]:
        if candidate not in search_roots and candidate.exists():
            search_roots.append(candidate)

    ranked: List[Path] = []
    for root in search_roots:
        for name in DEFAULT_DB_CANDIDATES:
            p = root / name
            if p.exists() and p not in ranked:
                ranked.append(p)
        for pat in ("*.sqlite", "*.db", "*.sqlite3"):
            for p in sorted(root.glob(pat)):
                if p not in ranked:
                    ranked.append(p)

    valid = [p for p in ranked if is_complaint_warrior_db(p)]
    if not valid:
        return None
    valid.sort(key=lambda p: (p.name.lower() not in [x.lower() for x in DEFAULT_DB_CANDIDATES], len(str(p))))
    return valid[0]


def ensure_outreach_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bank_dispute_outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            bank_name TEXT,
            bank_domain TEXT,
            recipient_name TEXT,
            recipient_email TEXT,
            recipient_source TEXT,
            transaction_amount TEXT,
            transaction_date TEXT,
            merchant_name TEXT,
            dispute_reason TEXT,
            subject TEXT,
            body TEXT,
            gmail_draft_id TEXT,
            gmail_message_id TEXT,
            status TEXT NOT NULL DEFAULT 'drafted',
            notes_json TEXT,
            UNIQUE(complaint_id, recipient_email, subject)
        )
        """
    )
    con.commit()


def connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    ensure_outreach_table(con)
    return con


def load_cases(con: sqlite3.Connection) -> List[ComplaintCase]:
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "complaints" not in tables:
        return []

    columns = {row[1] for row in con.execute("PRAGMA table_info(complaints)").fetchall()}
    if {"complaint_id", "complaint_json"}.issubset(columns):
        rows = con.execute(
            "SELECT complaint_id, complaint_json FROM complaints ORDER BY updated_at DESC"
        ).fetchall()
    elif "complaint_json" in columns:
        rows = con.execute(
            "SELECT rowid AS complaint_id, complaint_json FROM complaints ORDER BY rowid DESC"
        ).fetchall()
    else:
        return []
    cases: List[ComplaintCase] = []
    for complaint_id, complaint_json in rows:
        try:
            payload = json.loads(complaint_json)
        except Exception:
            continue
        cases.append(
            ComplaintCase(
                complaint_id=payload.get("complaint_id", complaint_id),
                subject=payload.get("subject", ""),
                complaint_raw=payload.get("complaint_raw", ""),
                complaint_professional=payload.get("complaint_professional", ""),
                current_status_summary=payload.get("current_status_summary", ""),
                final_conclusion=payload.get("final_conclusion", ""),
                created_at=payload.get("created_at", ""),
                user_email=payload.get("user_email", ""),
                user_name=payload.get("user_name", ""),
                docs=list(payload.get("docs") or []),
                evidence_pack_pdf=payload.get("evidence_pack_pdf"),
                activities=list(payload.get("activities") or []),
                threads=dict(payload.get("threads") or {}),
            )
        )
    return cases


def load_prior_outreach(con: sqlite3.Connection, complaint_id: str) -> List[dict]:
    cur = con.execute(
        """
        SELECT created_at, updated_at, bank_name, recipient_name, recipient_email,
               subject, status, gmail_draft_id, gmail_message_id
        FROM bank_dispute_outreach
        WHERE complaint_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (complaint_id,),
    )
    cols = [x[0] for x in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def save_outreach_record(
    con: sqlite3.Connection,
    *,
    complaint_id: str,
    bank_name: str,
    bank_domain: str,
    recipient_name: str,
    recipient_email: str,
    recipient_source: str,
    transaction_amount: str,
    transaction_date: str,
    merchant_name: str,
    dispute_reason: str,
    subject: str,
    body: str,
    status: str,
    gmail_draft_id: str = "",
    gmail_message_id: str = "",
    notes: Optional[dict] = None,
) -> None:
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    con.execute(
        """
        INSERT INTO bank_dispute_outreach (
            complaint_id, created_at, updated_at, bank_name, bank_domain,
            recipient_name, recipient_email, recipient_source, transaction_amount,
            transaction_date, merchant_name, dispute_reason, subject, body,
            gmail_draft_id, gmail_message_id, status, notes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(complaint_id, recipient_email, subject)
        DO UPDATE SET
            updated_at = excluded.updated_at,
            bank_name = excluded.bank_name,
            bank_domain = excluded.bank_domain,
            recipient_name = excluded.recipient_name,
            recipient_source = excluded.recipient_source,
            transaction_amount = excluded.transaction_amount,
            transaction_date = excluded.transaction_date,
            merchant_name = excluded.merchant_name,
            dispute_reason = excluded.dispute_reason,
            body = excluded.body,
            gmail_draft_id = excluded.gmail_draft_id,
            gmail_message_id = excluded.gmail_message_id,
            status = excluded.status,
            notes_json = excluded.notes_json
        """,
        (
            complaint_id, now, now, bank_name, bank_domain, recipient_name, recipient_email,
            recipient_source, transaction_amount, transaction_date, merchant_name,
            dispute_reason, subject, body, gmail_draft_id, gmail_message_id, status,
            json.dumps(notes or {}, ensure_ascii=False),
        ),
    )
    con.commit()


# -----------------------------
# Complaint parsing helpers
# -----------------------------
def summarize_case(case: ComplaintCase) -> str:
    text = case.complaint_raw or case.complaint_professional or ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600] + ("…" if len(text) > 600 else "")


def infer_bank_name(case: ComplaintCase) -> str:
    text = " ".join([
        case.subject,
        case.complaint_raw,
        case.complaint_professional,
        case.current_status_summary,
        case.final_conclusion,
    ]).lower()
    for name in COMMON_BANK_DOMAINS:
        if name in text:
            return name.title()
    patterns = [
        r"(?:from|with|at|through)\s+([A-Z][A-Za-z&.'\- ]{2,50}?\s+(?:Bank|Credit Union))",
        r"([A-Z][A-Za-z&.'\- ]{2,50}?\s+(?:Bank|Credit Union))",
    ]
    source = " ".join([case.subject, case.complaint_raw, case.complaint_professional])
    for pat in patterns:
        m = re.search(pat, source)
        if m:
            return m.group(1).strip()
    return ""


def infer_bank_domain(bank_name: str) -> str:
    if not bank_name:
        return ""
    normalized = bank_name.strip().lower()
    if normalized in COMMON_BANK_DOMAINS:
        return COMMON_BANK_DOMAINS[normalized]
    cleaned = re.sub(r"\b(bank|credit union|financial|wealth|investments?)\b", "", normalized)
    cleaned = re.sub(r"[^a-z0-9]+", "", cleaned)
    return f"{cleaned}.com" if cleaned else ""


def extract_first_amount(text: str) -> str:
    if not text:
        return ""
    for pat in TRANSACTION_PATTERNS:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1).replace(",", "")
    return ""


def extract_first_date(text: str) -> str:
    if not text:
        return ""
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(0)
    return ""


def infer_merchant_name(case: ComplaintCase) -> str:
    text = case.complaint_raw or ""
    patterns = [
        r"(?:transaction|charge|purchase)\s+(?:with|from|at)\s+([A-Z][A-Za-z0-9&.'\- ]{2,60})",
        r"(?:merchant|vendor)\s*[:\-]\s*([A-Z][A-Za-z0-9&.'\- ]{2,60})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip(" .,")
    return ""


def infer_dispute_reason(case: ComplaintCase) -> str:
    raw = (case.complaint_raw or "").strip()
    if not raw:
        return "Unresolved consumer dispute associated with this transaction."
    sentences = re.split(r"(?<=[.!?])\s+", raw)
    return " ".join(sentences[:3]).strip()


# -----------------------------
# Gmail / Contacts helpers
# -----------------------------
def google_client_ready() -> bool:
    return all([Request, Credentials, InstalledAppFlow, build])


def token_store_ready(token_db_path: Path) -> bool:
    if not token_db_path.exists():
        return False
    try:
        con = sqlite3.connect(str(token_db_path))
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gmail_tokens'"
        ).fetchone()
        con.close()
        return bool(row)
    except Exception:
        return False


def list_token_keys(token_db_path: Path) -> List[str]:
    if not token_store_ready(token_db_path):
        return []
    con = sqlite3.connect(str(token_db_path))
    try:
        rows = con.execute(
            "SELECT key FROM gmail_tokens ORDER BY CASE WHEN key='default' THEN 0 ELSE 1 END, updated_at DESC, key"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def ensure_token_store_table(token_db_path: Path) -> None:
    con = sqlite3.connect(str(token_db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_tokens (
                key TEXT PRIMARY KEY,
                token_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        con.commit()
    finally:
        con.close()


def get_token_record(token_db_path: Path, token_key: str) -> Optional[dict]:
    if not token_store_ready(token_db_path):
        return None
    con = sqlite3.connect(str(token_db_path))
    try:
        row = con.execute(
            "SELECT key, token_json, updated_at FROM gmail_tokens WHERE key = ?",
            (token_key,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    payload = json.loads(row[1])
    return {
        "key": row[0],
        "updated_at": row[2],
        "scopes": normalize_scopes(payload.get("scopes") or []),
        "has_refresh_token": bool(payload.get("refresh_token")),
        "client_id": payload.get("client_id", ""),
    }


def save_token_json_to_sqlite(token_db_path: Path, token_key: str, token_json: str) -> None:
    ensure_token_store_table(token_db_path)
    con = sqlite3.connect(str(token_db_path))
    try:
        con.execute(
            """
            INSERT INTO gmail_tokens(key, token_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                token_json = excluded.token_json,
                updated_at = excluded.updated_at
            """,
            (token_key, token_json, dt.datetime.utcnow().timestamp()),
        )
        con.commit()
    finally:
        con.close()


def load_token_json_from_sqlite(token_db_path: Path, token_key: str) -> dict:
    con = sqlite3.connect(str(token_db_path))
    try:
        row = con.execute(
            "SELECT token_json FROM gmail_tokens WHERE key = ?",
            (token_key,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise KeyError(f"Token key not found in {token_db_path.name}: {token_key}")
    payload = json.loads(row[0])
    if not isinstance(payload, dict):
        raise ValueError("Stored token_json is not a JSON object")
    return payload


def normalize_scopes(scopes: Optional[List[str]]) -> List[str]:
    values = list(scopes or [])
    return sorted(set(v for v in values if isinstance(v, str) and v.strip()))


def get_credentials_from_sqlite(token_db_path: Path, token_key: str) -> Credentials:
    if not google_client_ready():
        raise RuntimeError(
            "Google API packages are not installed. Install google-api-python-client, "
            "google-auth-oauthlib, and google-auth-httplib2."
        )
    info = load_token_json_from_sqlite(token_db_path, token_key)
    available_scopes = normalize_scopes(info.get("scopes") or [])
    scopes_for_creds = available_scopes or GMAIL_SCOPES
    creds = Credentials.from_authorized_user_info(info, scopes=scopes_for_creds)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_token_json_to_sqlite(token_db_path, token_key, creds.to_json())
            except Exception as exc:
                raise TokenRefreshError(
                    "The stored Gmail token in SQLite could not be refreshed. Google returned invalid_grant or another refresh error. "
                    "Re-authorize the selected token key to continue."
                ) from exc
        else:
            raise TokenRefreshError(
                "The stored Gmail token in SQLite is missing a usable refresh token. Re-authorize the selected token key to continue."
            )
    return creds


def reauthorize_token_in_sqlite(token_db_path: Path, token_key: str, credentials_path: Path) -> None:
    if not google_client_ready():
        raise RuntimeError(
            "Google API packages are not installed. Install google-api-python-client, google-auth-oauthlib, and google-auth-httplib2."
        )
    if not credentials_path.exists():
        raise FileNotFoundError(f"Missing Google OAuth client file: {credentials_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), ALL_APP_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    save_token_json_to_sqlite(token_db_path, token_key, creds.to_json())


def get_credentials_legacy(credentials_path: Path, token_path: Path) -> Credentials:
    if not google_client_ready():
        raise RuntimeError(
            "Google API packages are not installed. Install google-api-python-client, "
            "google-auth-oauthlib, and google-auth-httplib2."
        )
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), ALL_APP_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(f"Missing Google OAuth client file: {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), ALL_APP_SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_services(
    *,
    token_db_path: Optional[Path] = None,
    token_key: str = "default",
    credentials_path: Optional[Path] = None,
    token_path: Optional[Path] = None,
):
    """Build Gmail service and People service when available.

    Returns (gmail_service, people_service_or_None, scope_info)
    """
    if token_db_path and token_store_ready(token_db_path):
        creds = get_credentials_from_sqlite(token_db_path, token_key)
        scope_info = normalize_scopes(getattr(creds, "scopes", None) or [])
    else:
        if credentials_path is None or token_path is None:
            raise FileNotFoundError("No SQLite token store found and no legacy token paths were provided.")
        creds = get_credentials_legacy(credentials_path, token_path)
        scope_info = normalize_scopes(getattr(creds, "scopes", None) or [])

    gmail = build("gmail", "v1", credentials=creds)
    people = None
    if CONTACTS_SCOPE in scope_info:
        try:
            people = build("people", "v1", credentials=creds)
        except Exception:
            people = None
    return gmail, people, scope_info


def _parse_from_header(value: str) -> Tuple[str, str]:
    name, addr = email.utils.parseaddr(value or "")
    return (name.strip(), addr.strip().lower())


def _parse_address_list(value: str) -> Tuple[List[str], List[str]]:
    parsed = email.utils.getaddresses([value or ""])
    names: List[str] = []
    addresses: List[str] = []
    for name, address in parsed:
        address = (address or "").strip().lower()
        if not address:
            continue
        names.append((name or "").strip())
        addresses.append(address)
    return names, addresses


def _decode_gmail_body(payload: Dict[str, Any], limit: int = 1800) -> str:
    """Extract a short plain-text body excerpt from a Gmail message payload."""
    chunks: List[str] = []

    def decode_data(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def walk(part: Dict[str, Any]) -> None:
        if len("\n".join(chunks)) >= limit:
            return
        mime_type = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            value = decode_data(data)
            if mime_type == "text/html":
                value = re.sub(r"<[^>]+>", " ", value)
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                chunks.append(value)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return " ".join(chunks)[:limit].strip()


def build_keyword_query(keywords: Sequence[str]) -> str:
    terms = []
    for keyword in keywords:
        keyword = (keyword or "").strip()
        if not keyword:
            continue
        terms.append(f'"{keyword}"' if " " in keyword else keyword)
    return "{" + " ".join(terms) + "}" if terms else "{credit transaction}"


def search_gmail_messages(
    gmail_svc,
    *,
    keywords: Sequence[str],
    max_messages: int = 50,
) -> List[GmailSearchMessage]:
    """Search both sent and received mail for the configured charge-back keywords."""
    query = build_keyword_query(keywords)
    response = gmail_svc.users().messages().list(
        userId="me",
        q=query,
        maxResults=max(1, min(max_messages, 200)),
    ).execute()

    results: List[GmailSearchMessage] = []
    for ref in response.get("messages", []):
        full = gmail_svc.users().messages().get(
            userId="me",
            id=ref["id"],
            format="full",
        ).execute()
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (full.get("payload", {}).get("headers") or [])
        }
        from_name, from_email = _parse_from_header(headers.get("from", ""))
        to_names, to_emails = _parse_address_list(headers.get("to", ""))
        _cc_names, cc_emails = _parse_address_list(headers.get("cc", ""))
        results.append(
            GmailSearchMessage(
                message_id=full.get("id", ""),
                thread_id=full.get("threadId", ""),
                from_name=from_name,
                from_email=from_email,
                to_names=to_names,
                to_emails=to_emails,
                cc_emails=cc_emails,
                subject=headers.get("subject", ""),
                date=headers.get("date", ""),
                snippet=re.sub(r"\s+", " ", full.get("snippet", "")).strip()[:500],
                body_excerpt=_decode_gmail_body(full.get("payload") or {}, limit=1800),
            )
        )
    return results


def _extract_json_object(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("ChatGPT did not return a JSON object.")
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}


def select_personal_banker_from_gmail(
    messages: Sequence[GmailSearchMessage],
    user_email: str,
    bank_name: str,
    bank_domain: str,
) -> BankerSelection:
    """Use ChatGPT to identify a real personal banker from the Gmail result list."""

    if OpenAI is None:
        raise RuntimeError("The openai package is not installed. Run: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing [OpenAI] api_key in config.ini.")
    if not messages:
        raise ValueError("Gmail search returned no messages.")

    import re

    normalized_user = (user_email or "").strip().lower()
    bank_domain = (bank_domain or "").strip().lower().lstrip("@")

    def clean_email(e: str) -> str:
        return (e or "").strip().lower()

    def is_bad_address(email: str) -> bool:
        e = clean_email(email)
        bad_tokens = [
            "no-reply", "noreply", "donotreply", "do-not-reply",
            "alerts", "alert", "notification", "notifications",
            "fraudalert", "fraud-alert", "support", "customer",
            "service", "marketing", "promo", "offers", "receipt",
        ]
        return any(t in e for t in bad_tokens)

    def domain_bonus(email: str) -> float:
        e = clean_email(email)
        if bank_domain and e.endswith("@" + bank_domain):
            return 0.35
        # Merrill Lynch / Bank of America common domains
        if e.endswith("@ml.com") or e.endswith("@bofa.com") or e.endswith("@bankofamerica.com"):
            return 0.30
        return 0.0

    def infer_name_from_email(email: str) -> str:
        local = clean_email(email).split("@")[0]
        parts = re.split(r"[._-]+", local)
        return " ".join(p.capitalize() for p in parts if p)

    compact_messages: List[Dict[str, Any]] = []
    allowed_by_message: Dict[str, set[str]] = {}

    for item in messages:
        allowed = {
            clean_email(email_address)
            for email_address in [item.from_email, *item.to_emails, *item.cc_emails]
            if email_address and clean_email(email_address) != normalized_user
        }

        allowed_by_message[item.message_id] = allowed

        compact_messages.append(
            {
                "message_id": item.message_id,
                "from_name": item.from_name,
                "from_email": item.from_email,
                "to_names": item.to_names,
                "to_emails": item.to_emails,
                "cc_emails": item.cc_emails,
                "subject": item.subject,
                "date": item.date,
                "snippet": item.snippet,
                "body_excerpt": item.body_excerpt[:2500],
                "allowed_counterparty_emails": sorted(allowed),
            }
        )

    def deterministic_fallback(reason: str = "") -> BankerSelection:
        best = None

        banker_keywords = [
            "banker", "relationship manager", "private client",
            "financial advisor", "branch manager", "merrill",
            "fraudulent transaction", "suspicious activity",
        ]

        for item in messages:
            allowed = allowed_by_message.get(item.message_id, set())
            text = " ".join(
                [
                    item.from_name or "",
                    item.from_email or "",
                    " ".join(item.to_names or []),
                    " ".join(item.to_emails or []),
                    " ".join(item.cc_emails or []),
                    item.subject or "",
                    item.snippet or "",
                    item.body_excerpt or "",
                ]
            ).lower()

            for email in allowed:
                if is_bad_address(email):
                    continue

                score = 0.2
                score += domain_bonus(email)

                if any(k in text for k in banker_keywords):
                    score += 0.25

                if email == clean_email(item.from_email):
                    direction = "from_banker"
                    score += 0.15
                elif email in {clean_email(e) for e in item.to_emails}:
                    direction = "to_banker"
                    score += 0.10
                else:
                    direction = "unknown"

                if best is None or score > best[0]:
                    name = item.from_name if email == clean_email(item.from_email) else infer_name_from_email(email)

                    # Special case: "Edgar, William" style from/to headers
                    if "," in name:
                        last, first = [p.strip() for p in name.split(",", 1)]
                        name = f"{first} {last}".strip()

                    best = (score, item.message_id, name, email, direction)

        if not best or best[0] < 0.35:
            return BankerSelection("", "", "", "unknown", 0.0, reason or "No credible personal banker was found.")

        score, message_id, name, email, direction = best

        return BankerSelection(
            selected_message_id=message_id,
            banker_name=name,
            banker_email=email,
            direction=direction,
            confidence=max(0.0, min(score, 1.0)),
            reason=reason or "Selected by deterministic fallback from Gmail headers and bank-domain match.",
        )

    system_prompt = """You select a personal banker or relationship manager from Gmail search results.
Return strict JSON only.

Select one message that is most likely a direct human email to or from the user's personal banker,
relationship manager, private-client banker, branch manager, financial advisor, or similar named bank employee.

Important:
- A valid banker may appear in the To field if the user replied to them.
- A valid banker may appear in quoted email text inside a reply thread.
- Prefer named human employees at Merrill Lynch, Bank of America, or the provided bank domain.
- Reject automated alerts, fraud-alert robots, card notifications, receipts, marketing, customer-service queues,
  generic support addresses, no-reply addresses, and merchant emails.

The selected banker_email MUST be copied exactly from allowed_counterparty_emails for the selected message.
Never invent, modify, infer, or autocomplete an email address. If no credible personal banker is present,
return empty strings and confidence 0.

Return:
{
  "selected_message_id": "",
  "banker_name": "",
  "banker_email": "",
  "direction": "from_banker|to_banker|unknown",
  "confidence": 0.0,
  "reason": ""
}
"""

    payload = {
        "user_email": normalized_user,
        "bank_name_hint": bank_name,
        "bank_domain_hint": bank_domain,
        "messages": compact_messages,
    }

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        parsed = _extract_json_object(response.choices[0].message.content or "{}")
    except Exception as exc:
        return deterministic_fallback(f"OpenAI selection failed; used fallback. Error: {exc}")

    message_id = str(parsed.get("selected_message_id") or "").strip()
    banker_email = clean_email(parsed.get("banker_email") or "")
    banker_name = str(parsed.get("banker_name") or "").strip()
    direction = str(parsed.get("direction") or "unknown").strip()
    reason = str(parsed.get("reason") or "").strip()

    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    if not message_id or message_id not in allowed_by_message:
        return deterministic_fallback(reason or "ChatGPT did not select a valid message.")

    if not banker_email or banker_email not in allowed_by_message[message_id]:
        return deterministic_fallback(
            reason or "ChatGPT selected an email not present in the selected message headers."
        )

    if is_bad_address(banker_email):
        return deterministic_fallback(reason or "ChatGPT selected an automated or generic address.")

    return BankerSelection(
        selected_message_id=message_id,
        banker_name=banker_name or infer_name_from_email(banker_email),
        banker_email=banker_email,
        direction=direction if direction in {"from_banker", "to_banker", "unknown"} else "unknown",
        confidence=max(0.0, min(confidence, 1.0)),
        reason=reason,
    )


def verify_banker_bank_card_match(
    *,
    banker_email: str,
    bank_name: str,
    bank_domain: str,
    transaction_card_bank: str,
) -> BankerTransactionVerification:
    """Verify whether the identified banker appears to belong to the card-issuing bank.

    This does NOT prove the disputed transaction used that bank card. It only verifies
    that the selected banker email/domain is consistent with the bank/card issuer the
    user entered for the disputed transaction. The app still requires explicit user
    confirmation before saving, drafting, or sending.
    """
    email = (banker_email or "").strip().lower()
    card_bank = (transaction_card_bank or "").strip().lower()
    bank_name_norm = (bank_name or "").strip().lower()
    bank_domain_norm = (bank_domain or "").strip().lower().lstrip("@")

    if not email:
        return BankerTransactionVerification(False, 0.0, "No banker email was selected.", "")
    if "@" not in email:
        return BankerTransactionVerification(False, 0.0, "Selected banker email is malformed.", email)

    email_domain = email.rsplit("@", 1)[1]
    accepted_domains = {d for d in [bank_domain_norm] if d}

    # Merrill Lynch addresses are Bank of America / Merrill related and often used by advisors.
    bofa_terms = {"bank of america", "bofa", "boa", "merrill", "merrill lynch"}
    if any(term in card_bank or term in bank_name_norm for term in bofa_terms):
        accepted_domains.update({"ml.com", "bofa.com", "bankofamerica.com", "merrilledge.com"})

    if bank_name_norm in COMMON_BANK_DOMAINS:
        accepted_domains.add(COMMON_BANK_DOMAINS[bank_name_norm])

    if card_bank in COMMON_BANK_DOMAINS:
        accepted_domains.add(COMMON_BANK_DOMAINS[card_bank])

    if email_domain in accepted_domains:
        return BankerTransactionVerification(
            True,
            0.9,
            "Selected banker email domain matches the entered card-issuing bank/domain.",
            f"{email_domain} in {sorted(accepted_domains)}",
        )

    # Softer text match fallback, useful when the bank domain was not configured.
    if card_bank and any(token for token in re.split(r"[^a-z0-9]+", card_bank) if token and token in email_domain):
        return BankerTransactionVerification(
            True,
            0.55,
            "Selected banker email domain partially matches the entered card-issuing bank name.",
            f"{card_bank} vs {email_domain}",
        )

    return BankerTransactionVerification(
        False,
        0.0,
        "Selected banker email domain does not match the entered card-issuing bank/domain.",
        f"email_domain={email_domain}; accepted_domains={sorted(accepted_domains)}; card_bank={transaction_card_bank}",
    )


def require_card_confirmation(
    *,
    recipient_email: str,
    bank_name: str,
    bank_domain: str,
    transaction_card_bank: str,
    user_confirmed_card_bank: bool,
) -> Tuple[bool, str, BankerTransactionVerification]:
    """Return whether the action may continue, plus a user-facing message.

    The confirmation checkbox is still required. After the user confirms, a
    mismatch between the selected banker and the entered card/bank domain is
    treated as a warning only, not as a hard blocker. This allows the user to
    manually override false negatives such as Merrill Lynch / Bank of America
    advisor domains or manually confirmed recipients.
    """
    verification = verify_banker_bank_card_match(
        banker_email=recipient_email,
        bank_name=bank_name,
        bank_domain=bank_domain,
        transaction_card_bank=transaction_card_bank,
    )
    if not user_confirmed_card_bank:
        return (
            False,
            "Please confirm that the disputed transaction was made on a card issued by the identified bank/banker before continuing.",
            verification,
        )
    if not verification.verified:
        return (
            True,
            "Warning: the selected banker does not verify against the entered card-issuing bank/domain. User confirmed anyway; continuing.",
            verification,
        )
    return True, "", verification


def search_people_candidates(people_svc, bank_name: str, bank_domain: str) -> List[GmailCandidate]:
    candidates: List[GmailCandidate] = []
    if people_svc is None:
        return candidates
    query = bank_name or bank_domain or "bank"
    try:
        resp = people_svc.people().searchContacts(
            query=query,
            readMask="names,emailAddresses,organizations",
            pageSize=20,
        ).execute()
    except Exception:
        return candidates

    for result in resp.get("results", []):
        person = result.get("person", {})
        names = person.get("names") or []
        orgs = person.get("organizations") or []
        emails = person.get("emailAddresses") or []
        display = names[0].get("displayName", "") if names else ""
        org_name = orgs[0].get("name", "") if orgs else ""
        for email_obj in emails:
            addr = (email_obj.get("value") or "").strip().lower()
            if not addr:
                continue
            score = 0.5
            hay = f"{display} {org_name} {addr}".lower()
            if bank_name and bank_name.lower() in hay:
                score += 2.0
            if bank_domain and addr.endswith("@" + bank_domain):
                score += 3.0
            candidates.append(
                GmailCandidate(
                    source="contacts",
                    name=display,
                    email=addr,
                    last_seen="",
                    message_count=1,
                    score=score,
                    sample_subjects=[org_name] if org_name else [],
                )
            )

    dedup: Dict[str, GmailCandidate] = {}
    for item in candidates:
        current = dedup.get(item.email)
        if current is None or item.score > current.score:
            dedup[item.email] = item
    return sorted(dedup.values(), key=lambda x: (-x.score, x.email))


def build_mime_message(sender: str, to: str, subject: str, body: str, cc: str = "") -> str:
    from email.mime.text import MIMEText

    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    if cc.strip():
        message["cc"] = cc.strip()
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def get_profile_email(gmail_svc) -> str:
    profile = gmail_svc.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def create_gmail_draft(gmail_svc, raw_message: str) -> str:
    draft = gmail_svc.users().drafts().create(userId="me", body={"message": {"raw": raw_message}}).execute()
    return draft.get("id", "")


def send_gmail_message(gmail_svc, raw_message: str) -> str:
    msg = gmail_svc.users().messages().send(userId="me", body={"raw": raw_message}).execute()
    return msg.get("id", "")


# -----------------------------
# Draft generation
# -----------------------------
def compose_chargeback_email(
    *,
    recipient_name: str,
    bank_name: str,
    complaint: ComplaintCase,
    transaction_amount: str,
    transaction_date: str,
    merchant_name: str,
    dispute_reason: str,
    requested_action: str,
    include_case_id: bool,
) -> Tuple[str, str]:
    greeting_name = recipient_name or "Dispute Resolution Team"
    subject_parts = ["Charge-back request"]
    if merchant_name:
        subject_parts.append(merchant_name)
    if transaction_amount:
        subject_parts.append(f"${transaction_amount}")
    subject = " | ".join(subject_parts)

    lines = [f"Hello {greeting_name},", ""]
    lines.append("I am requesting a charge-back or payment-reversal review for a card transaction associated with a consumer complaint I have documented.")
    lines.append("")
    if bank_name:
        lines.append(f"Bank: {bank_name}")
    if transaction_amount:
        lines.append(f"Transaction amount: ${transaction_amount}")
    if transaction_date:
        lines.append(f"Transaction date: {transaction_date}")
    if merchant_name:
        lines.append(f"Merchant: {merchant_name}")
    if include_case_id:
        lines.append(f"Reference complaint ID: {complaint.complaint_id}")
    lines.append("")
    lines.append("Reason for charge-back request:")
    lines.append(dispute_reason.strip() or infer_dispute_reason(complaint))
    lines.append("")
    lines.append("Complaint summary:")
    lines.append(summarize_case(complaint))
    lines.append("")
    lines.append("Requested action:")
    lines.append(requested_action.strip() or "Please review this card transaction for charge-back or payment-reversal eligibility and confirm any documents you need from me to proceed.")
    lines.append("")
    if complaint.docs or complaint.evidence_pack_pdf:
        lines.append("I can provide supporting documents and the complaint evidence pack on request.")
        lines.append("")
    lines.append("Please confirm receipt and the next steps for the charge-back or payment reversal review.")
    lines.append("")
    sender_name = complaint.user_name or "Customer"
    sender_email = complaint.user_email or ""
    lines.append(sender_name)
    if sender_email:
        lines.append(sender_email)
    body = "\n".join(lines).strip() + "\n"
    return subject, body


# -----------------------------
# Streamlit UI
# -----------------------------
def init_state() -> None:
    st.session_state.setdefault("credentials_path", str(CONFIG_CREDENTIALS_PATH))
    st.session_state.setdefault("token_path", str(CONFIG_TOKEN_PATH))
    st.session_state.setdefault("token_db_path", str(CONFIG_TOKEN_DB_PATH))
    st.session_state.setdefault("token_key", CONFIG_TOKEN_KEY)
    st.session_state.setdefault("complaint_db_path", str(CONFIG_DB_PATH))
    st.session_state.setdefault("gmail_search_results", [])
    st.session_state.setdefault("banker_selection", {})
    st.session_state.setdefault("recipient_name", "")
    st.session_state.setdefault("recipient_email", "")
    st.session_state.setdefault("recipient_source", "manual")
    st.session_state.setdefault("transaction_card_bank", "")
    st.session_state.setdefault("confirm_card_bank_match", False)


def render_case_picker(cases: Sequence[ComplaintCase]) -> ComplaintCase:
    lookup = {
        f"{c.complaint_id} | {c.subject or '(no subject)'} | {c.created_at[:10] if c.created_at else 'unknown'}": c
        for c in cases
    }
    selected_label = st.selectbox("Complaint", list(lookup.keys()))
    return lookup[selected_label]


def get_token_key_options(token_db_path: Path) -> List[str]:
    try:
        keys = list_token_keys(token_db_path)
        return keys or ["default"]
    except Exception:
        return ["default"]


def app_main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_state()
    st.title(APP_TITLE)
    st.caption("Pre-small-claims charge-back initiation using your Complaint Warrior database plus your own Gmail and contacts.")

    db_path = Path(st.session_state.complaint_db_path)
    token_db_path = Path(st.session_state.token_db_path)
    token_store_exists = token_store_ready(token_db_path)
    token_key_options = get_token_key_options(token_db_path) if token_store_exists else [CONFIG_TOKEN_KEY or "default"]
    if st.session_state.token_key not in token_key_options:
        st.session_state.token_key = token_key_options[0]

    with st.sidebar:
        st.header("Configuration status")
        st.caption(f"Loaded from: {CONFIG_PATH}")
        st.caption(f"Complaint DB: {db_path}")
        st.caption(f"Gmail token DB: {token_db_path}")
        st.caption(f"Gmail token key: {st.session_state.token_key}")
        st.caption(f"OpenAI model: {OPENAI_MODEL}")
        st.caption("Search keywords: " + ", ".join(SEARCH_KEYWORDS))

        if OPENAI_API_KEY:
            st.success("OpenAI API key loaded.")
        else:
            st.error("Missing [OpenAI] api_key in config.ini.")

        if token_store_exists:
            st.success("Gmail SQLite token store found.")
            token_record = get_token_record(token_db_path, st.session_state.token_key)
            if token_record:
                updated_at = (
                    dt.datetime.fromtimestamp(token_record["updated_at"]).isoformat(timespec="seconds")
                    if token_record.get("updated_at")
                    else "unknown"
                )
                st.caption(f"Token updated: {updated_at}")
                st.caption("Scopes: " + (", ".join(token_record.get("scopes") or []) or "(none)"))
                st.caption(f"Refresh token present: {'yes' if token_record.get('has_refresh_token') else 'no'}")
            if st.button("Re-authorize Gmail token"):
                try:
                    reauthorize_token_in_sqlite(
                        token_db_path,
                        st.session_state.token_key,
                        Path(st.session_state.credentials_path),
                    )
                    st.success("Re-authorization complete.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not re-authorize token: {exc}")
        else:
            st.warning("Gmail SQLite token store not found; legacy token file will be used if available.")

    if not db_path.exists():
        st.error("Complaint Warrior database file was not found. Check [Database] path in config.ini.")
        return
    if not is_complaint_warrior_db(db_path):
        st.error("The configured SQLite file does not contain the Complaint Warrior complaint schema. Check [Database] path in config.ini.")
        return

    con = connect_db(db_path)
    cases = load_cases(con)
    if not cases:
        st.warning("No complaints found in the Complaint Warrior database.")
        return

    complaint = render_case_picker(cases)

    c1, c2 = st.columns([1.25, 1.0])
    with c1:
        st.subheader("Complaint context")
        st.markdown(f"**Subject:** {complaint.subject or '(none)'}")
        st.markdown(f"**Status:** {complaint.current_status_summary or '(none)'}")
        st.markdown(f"**Conclusion:** {complaint.final_conclusion or '(none)'}")
        st.text_area("Complaint summary", value=summarize_case(complaint), height=220, disabled=True)
    with c2:
        st.subheader("Prior outreach")
        prior = load_prior_outreach(con, complaint.complaint_id)
        if prior:
            for row in prior[:8]:
                with st.container(border=True):
                    st.markdown(f"**{row['status']}** — {row['recipient_email']}")
                    st.caption(f"{row['updated_at']} | {row['subject']}")
        else:
            st.caption("No saved charge-back outreach yet.")

    inferred_bank = infer_bank_name(complaint)
    inferred_domain = infer_bank_domain(inferred_bank)
    inferred_amount = extract_first_amount(" ".join([complaint.complaint_raw, complaint.final_conclusion]))
    inferred_date = extract_first_date(complaint.complaint_raw)
    inferred_merchant = infer_merchant_name(complaint)
    inferred_reason = infer_dispute_reason(complaint)

    st.subheader("Charge-back details")
    d1, d2, d3 = st.columns(3)
    with d1:
        bank_name = st.text_input("Bank name", value=inferred_bank)
        bank_domain = st.text_input("Bank domain", value=inferred_domain)
        default_card_bank = st.session_state.transaction_card_bank or bank_name
        transaction_card_bank = st.text_input(
            "Card-issuing bank for this disputed transaction",
            value=default_card_bank,
            help="Required confirmation: this must be the bank whose card was used for the disputed transaction.",
        )
        st.session_state.transaction_card_bank = transaction_card_bank
    with d2:
        transaction_amount = st.text_input("Transaction amount", value=inferred_amount)
        transaction_date = st.text_input("Transaction date", value=inferred_date)
    with d3:
        merchant_name = st.text_input("Merchant name", value=inferred_merchant)
        include_case_id = st.checkbox("Include complaint ID in email", value=True)

    dispute_reason = st.text_area("Charge-back reason", value=inferred_reason, height=140)
    requested_action = st.text_area(
        "Requested action",
        value="Please review this card transaction for charge-back or payment-reversal eligibility, explain the formal process, and let me know what additional documentation you need from me.",
        height=100,
    )

    st.subheader("Find your personal banker in Gmail")
    st.caption(
        "Gmail search keywords from config.ini: "
        + ", ".join(SEARCH_KEYWORDS)
        + ". ChatGPT then selects one direct message to or from a likely personal banker and copies the banker email from the message headers."
    )

    if st.button("Search Gmail and select personal banker with ChatGPT", type="primary"):
        try:
            gmail_svc, _people_svc, scope_info = build_services(
                token_db_path=Path(st.session_state.token_db_path),
                token_key=st.session_state.token_key,
                credentials_path=Path(st.session_state.credentials_path),
                token_path=Path(st.session_state.token_path),
            )
            profile_email = get_profile_email(gmail_svc)
            search_results = search_gmail_messages(
                gmail_svc,
                keywords=SEARCH_KEYWORDS,
                max_messages=MAX_GMAIL_SEARCH_RESULTS,
            )
            st.session_state["gmail_search_results"] = [asdict(item) for item in search_results]
            st.session_state["active_scope_info"] = scope_info

            selection = select_personal_banker_from_gmail(
                messages=search_results,
                user_email=profile_email,
                bank_name=bank_name,
                bank_domain=bank_domain,
            )
            st.session_state["banker_selection"] = asdict(selection)

            if selection.banker_email:
                st.session_state["recipient_name"] = selection.banker_name
                st.session_state["recipient_email"] = selection.banker_email
                st.session_state["recipient_source"] = (
                    f"gmail_chatgpt:{selection.selected_message_id}"
                )
                st.success(
                    f"Selected personal banker: {selection.banker_name or '(name unavailable)'} "
                    f"<{selection.banker_email}>"
                )
            else:
                st.warning("ChatGPT did not find a credible personal banker in the Gmail results.")
        except TokenRefreshError as exc:
            st.error(str(exc))
            st.info("Use the sidebar re-authorization button and try again.")
        except Exception as exc:
            st.error(f"Could not search Gmail or select a personal banker: {exc}")

    active_scopes = st.session_state.get("active_scope_info", [])
    if active_scopes:
        st.caption("Active Gmail token scopes: " + ", ".join(active_scopes))

    stored_messages = [
        GmailSearchMessage(**item)
        for item in st.session_state.get("gmail_search_results", [])
    ]
    if stored_messages:
        st.markdown(f"**Gmail search results: {len(stored_messages)}**")
        st.dataframe(
            [
                {
                    "message_id": item.message_id,
                    "from": f"{item.from_name} <{item.from_email}>".strip(),
                    "to": ", ".join(item.to_emails),
                    "subject": item.subject,
                    "date": item.date,
                    "snippet": item.snippet[:220],
                }
                for item in stored_messages
            ],
            use_container_width=True,
            hide_index=True,
        )

    selection_data = st.session_state.get("banker_selection") or {}
    if selection_data:
        selection = BankerSelection(**selection_data)
        with st.container(border=True):
            st.markdown("**ChatGPT personal-banker selection**")
            st.write(f"Name: {selection.banker_name or '(not found)'}")
            st.write(f"Email: {selection.banker_email or '(not found)'}")
            st.write(f"Direction: {selection.direction}")
            st.write(f"Confidence: {selection.confidence:.2f}")
            st.caption(selection.reason or "No explanation supplied.")

    st.subheader("Required bank-card verification")
    current_recipient_email = st.session_state.get("recipient_email", "")
    verification = verify_banker_bank_card_match(
        banker_email=current_recipient_email,
        bank_name=bank_name,
        bank_domain=bank_domain,
        transaction_card_bank=st.session_state.get("transaction_card_bank", "") or bank_name,
    )
    if verification.verified:
        st.success(f"Banker/card-bank verification passed: {verification.reason}")
    else:
        st.warning(f"Banker/card-bank verification not passed: {verification.reason}")
        if verification.evidence:
            st.caption(verification.evidence)

    profile_user_email = (complaint.user_email or "").strip().lower()
    if current_recipient_email and profile_user_email and current_recipient_email.strip().lower() == profile_user_email:
        st.warning("Warning: the selected recipient is your own email address. You may still continue after confirming.")

    confirm_label = (
        "I confirm that the disputed transaction was made on a card issued by "
        f"{st.session_state.get('transaction_card_bank', '') or bank_name or 'the bank above'}, "
        f"and that {current_recipient_email or 'the selected recipient'} is the banker/advisor for that bank."
    )
    confirm_card_bank_match = st.checkbox(
        confirm_label,
        key="confirm_card_bank_match",
        value=bool(st.session_state.get("confirm_card_bank_match", False)),
    )

    st.subheader("Recipient and draft")
    r1, r2 = st.columns([1.1, 0.9])
    with r1:
        recipient_name = st.text_input("Recipient name", key="recipient_name")
        recipient_email = st.text_input("Recipient email", key="recipient_email")
        recipient_source = st.text_input("Recipient source", key="recipient_source")
        cc_email = st.text_input("CC (optional)", value=complaint.user_email or "")
    with r2:
        st.caption(
            "The selected email is copied from the From/To/CC headers of the Gmail message chosen by ChatGPT. "
            "You can still review or replace it manually before creating a draft or sending."
        )

    subject, body = compose_chargeback_email(
        recipient_name=recipient_name,
        bank_name=bank_name,
        complaint=complaint,
        transaction_amount=transaction_amount,
        transaction_date=transaction_date,
        merchant_name=merchant_name,
        dispute_reason=dispute_reason,
        requested_action=requested_action,
        include_case_id=include_case_id,
    )

    subject = st.text_input("Email subject", value=subject)
    body = st.text_area("Email body", value=body, height=320)

    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("Save draft record to DB"):
            ok, err, card_verification = require_card_confirmation(
                recipient_email=recipient_email,
                bank_name=bank_name,
                bank_domain=bank_domain,
                transaction_card_bank=st.session_state.get("transaction_card_bank", "") or bank_name,
                user_confirmed_card_bank=bool(st.session_state.get("confirm_card_bank_match", False)),
            )
            if not recipient_email.strip():
                st.error("Recipient email is required.")
            elif not ok:
                st.error(err)
            else:
                if err:
                    st.warning(err)
                save_outreach_record(
                    con,
                    complaint_id=complaint.complaint_id,
                    bank_name=bank_name,
                    bank_domain=bank_domain,
                    recipient_name=recipient_name,
                    recipient_email=recipient_email,
                    recipient_source=recipient_source,
                    transaction_amount=transaction_amount,
                    transaction_date=transaction_date,
                    merchant_name=merchant_name,
                    dispute_reason=dispute_reason,
                    subject=subject,
                    body=body,
                    status="drafted",
                    notes={"cc": cc_email, "token_key": st.session_state.token_key, "card_bank_verification": asdict(card_verification)},
                )
                st.success("Saved draft record into bank_dispute_outreach.")
    with a2:
        if st.button("Create Gmail draft"):
            ok, err, card_verification = require_card_confirmation(
                recipient_email=recipient_email,
                bank_name=bank_name,
                bank_domain=bank_domain,
                transaction_card_bank=st.session_state.get("transaction_card_bank", "") or bank_name,
                user_confirmed_card_bank=bool(st.session_state.get("confirm_card_bank_match", False)),
            )
            if not recipient_email.strip():
                st.error("Recipient email is required.")
            elif not ok:
                st.error(err)
            else:
                if err:
                    st.warning(err)
                try:
                    gmail_svc, _people, _scopes = build_services(
                        token_db_path=Path(st.session_state.token_db_path),
                        token_key=st.session_state.token_key,
                        credentials_path=Path(st.session_state.credentials_path),
                        token_path=Path(st.session_state.token_path),
                    )
                    sender = get_profile_email(gmail_svc)
                    raw = build_mime_message(sender, recipient_email, subject, body, cc_email)
                    draft_id = create_gmail_draft(gmail_svc, raw)
                    save_outreach_record(
                        con,
                        complaint_id=complaint.complaint_id,
                        bank_name=bank_name,
                        bank_domain=bank_domain,
                        recipient_name=recipient_name,
                        recipient_email=recipient_email,
                        recipient_source=recipient_source,
                        transaction_amount=transaction_amount,
                        transaction_date=transaction_date,
                        merchant_name=merchant_name,
                        dispute_reason=dispute_reason,
                        subject=subject,
                        body=body,
                        status="gmail_draft",
                        gmail_draft_id=draft_id,
                        notes={"cc": cc_email, "token_key": st.session_state.token_key, "card_bank_verification": asdict(card_verification)},
                    )
                    st.success(f"Created Gmail draft: {draft_id}")
                except TokenRefreshError as exc:
                    st.error(str(exc))
                    st.info("Use the sidebar button 'Re-authorize selected Gmail token' and then try again.")
                except Exception as exc:
                    st.error(f"Could not create Gmail draft: {exc}")
    with a3:
        if st.button("Send email now"):
            ok, err, card_verification = require_card_confirmation(
                recipient_email=recipient_email,
                bank_name=bank_name,
                bank_domain=bank_domain,
                transaction_card_bank=st.session_state.get("transaction_card_bank", "") or bank_name,
                user_confirmed_card_bank=bool(st.session_state.get("confirm_card_bank_match", False)),
            )
            if not recipient_email.strip():
                st.error("Recipient email is required.")
            elif not ok:
                st.error(err)
            else:
                if err:
                    st.warning(err)
                try:
                    gmail_svc, _people, _scopes = build_services(
                        token_db_path=Path(st.session_state.token_db_path),
                        token_key=st.session_state.token_key,
                        credentials_path=Path(st.session_state.credentials_path),
                        token_path=Path(st.session_state.token_path),
                    )
                    sender = get_profile_email(gmail_svc)
                    raw = build_mime_message(sender, recipient_email, subject, body, cc_email)
                    message_id = send_gmail_message(gmail_svc, raw)
                    save_outreach_record(
                        con,
                        complaint_id=complaint.complaint_id,
                        bank_name=bank_name,
                        bank_domain=bank_domain,
                        recipient_name=recipient_name,
                        recipient_email=recipient_email,
                        recipient_source=recipient_source,
                        transaction_amount=transaction_amount,
                        transaction_date=transaction_date,
                        merchant_name=merchant_name,
                        dispute_reason=dispute_reason,
                        subject=subject,
                        body=body,
                        status="sent",
                        gmail_message_id=message_id,
                        notes={"cc": cc_email, "token_key": st.session_state.token_key, "card_bank_verification": asdict(card_verification)},
                    )
                    st.success(f"Sent email. Gmail message id: {message_id}")
                except TokenRefreshError as exc:
                    st.error(str(exc))
                    st.info("Use the sidebar button 'Re-authorize selected Gmail token' and then try again.")
                except Exception as exc:
                    st.error(f"Could not send Gmail message: {exc}")

    st.divider()
    st.caption("This app is intended for reviewed, case-specific charge-back outreach using your own mailbox and contacts before escalating to Small Claim Court Warrior.")


if __name__ == "__main__":
    app_main()
