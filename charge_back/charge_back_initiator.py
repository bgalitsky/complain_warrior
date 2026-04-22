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


def search_gmail_candidates(
    gmail_svc,
    *,
    bank_name: str,
    bank_domain: str,
    extra_terms: str,
    max_messages: int = 40,
) -> List[GmailCandidate]:
    queries = []
    if bank_domain:
        queries.append(f"from:*@{bank_domain}")
    if bank_name:
        queries.append(f'"{bank_name}"')
    if extra_terms:
        queries.append(extra_terms)
    q = " OR ".join(x for x in queries if x).strip() or "bank OR transaction OR dispute"

    candidate_map: Dict[str, GmailCandidate] = {}
    resp = gmail_svc.users().messages().list(userId="me", q=q, maxResults=max_messages).execute()
    for msg in resp.get("messages", []):
        full = gmail_svc.users().messages().get(
            userId="me", id=msg["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        header_map = {h.get("name", ""): h.get("value", "") for h in full.get("payload", {}).get("headers", [])}
        name, addr = _parse_from_header(header_map.get("From", ""))
        if not addr:
            continue
        subject = header_map.get("Subject", "")
        date = header_map.get("Date", "")
        score = 0.0
        lower = f"{name} {addr} {subject}".lower()
        if bank_domain and addr.endswith("@" + bank_domain):
            score += 4.0
        if bank_name and bank_name.lower() in lower:
            score += 2.0
        if any(term in lower for term in ["advisor", "manager", "wealth", "relationship", "private client", "banker"]):
            score += 2.5
        if any(term in lower for term in ["dispute", "fraud", "claims", "service", "support"]):
            score += 1.0
        if addr not in candidate_map:
            candidate_map[addr] = GmailCandidate(
                source="gmail",
                name=name,
                email=addr,
                last_seen=date,
                message_count=0,
                score=0.0,
                sample_subjects=[],
            )
        item = candidate_map[addr]
        item.message_count += 1
        item.score += score + min(item.message_count * 0.2, 1.0)
        item.last_seen = item.last_seen or date
        if subject and subject not in item.sample_subjects and len(item.sample_subjects) < 3:
            item.sample_subjects.append(subject)

    return sorted(candidate_map.values(), key=lambda x: (-x.score, -x.message_count, x.email))


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
    st.session_state.setdefault("credentials_path", str(Path.cwd() / "credentials.json"))
    st.session_state.setdefault("token_path", str(Path.cwd() / "token.json"))
    default_token_db = discover_token_db_path()
    st.session_state.setdefault("token_db_path", str(default_token_db) if default_token_db else str(Path.cwd() / "cw_gmail_tokens.sqlite"))
    st.session_state.setdefault("token_key", "default")


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

    discovered_db_path = discover_db_path()
    st.session_state.setdefault("complaint_db_path", str(discovered_db_path) if discovered_db_path else str(Path.cwd() / "cw_store.sqlite"))
    db_path = Path(st.session_state.complaint_db_path)

    token_db_path = Path(st.session_state.token_db_path)
    token_store_exists = token_store_ready(token_db_path)
    token_key_options = get_token_key_options(token_db_path) if token_store_exists else ["default"]
    if st.session_state.token_key not in token_key_options:
        st.session_state.token_key = token_key_options[0]

    st.sidebar.header("Data sources")
    st.sidebar.text_input("Complaint Warrior DB", key="complaint_db_path")
    if db_path.exists() and is_complaint_warrior_db(db_path):
        st.sidebar.success(f"Using Complaint DB: {db_path}")
    elif db_path.exists():
        st.sidebar.error(f"Selected DB does not look like a Complaint Warrior case database: {db_path.name}")
    else:
        st.sidebar.warning(f"Complaint DB file not found: {db_path}")
    st.sidebar.text_input("SQLite Gmail token DB", key="token_db_path")
    st.sidebar.text_input("Google OAuth client JSON", key="credentials_path")
    if token_store_exists:
        st.sidebar.selectbox("SQLite token key", token_key_options, key="token_key")
        st.sidebar.success("Using Gmail token from SQLite store.")
        token_record = get_token_record(token_db_path, st.session_state.token_key)
        if token_record:
            updated_at = dt.datetime.fromtimestamp(token_record["updated_at"]).isoformat(timespec="seconds") if token_record.get("updated_at") else "unknown"
            st.sidebar.caption(f"Token updated: {updated_at}")
            st.sidebar.caption("Scopes: " + (", ".join(token_record.get("scopes") or []) or "(none)"))
            st.sidebar.caption(f"Refresh token present: {'yes' if token_record.get('has_refresh_token') else 'no'}")
        if st.sidebar.button("Re-authorize selected Gmail token"):
            try:
                reauthorize_token_in_sqlite(token_db_path, st.session_state.token_key, Path(st.session_state.credentials_path))
                st.sidebar.success("Re-authorization complete. Fresh token saved into SQLite.")
                st.rerun()
            except Exception as exc:
                st.sidebar.error(f"Could not re-authorize token: {exc}")
    else:
        st.sidebar.warning("No SQLite token store found. Falling back to legacy credentials/token JSON fields.")
        st.sidebar.text_input("Google token JSON", key="token_path")
    st.sidebar.caption("This app searches your own Gmail and, when the token includes contacts scope, your Google Contacts before sending.")

    if not db_path.exists():
        st.error("Complaint Warrior database file was not found. Place cw_store.sqlite in this folder or choose the correct file in the sidebar.")
        return
    if not is_complaint_warrior_db(db_path):
        st.error("Selected SQLite file does not contain the Complaint Warrior complaint schema. Pick the cw_store.sqlite database instead of the Gmail token store.")
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

    st.subheader("Find likely bank recipients from your Gmail / Contacts")
    extra_terms = st.text_input(
        "Extra Gmail search terms",
        value="advisor OR manager OR banker OR relationship OR wealth OR dispute",
    )

    if st.button("Search my Gmail and Contacts", type="primary"):
        try:
            gmail_svc, people_svc, scope_info = build_services(
                token_db_path=Path(st.session_state.token_db_path),
                token_key=st.session_state.token_key,
                credentials_path=Path(st.session_state.credentials_path),
                token_path=Path(st.session_state.token_path),
            )
            gmail_candidates = search_gmail_candidates(
                gmail_svc,
                bank_name=bank_name,
                bank_domain=bank_domain,
                extra_terms=extra_terms,
            )
            people_candidates = search_people_candidates(people_svc, bank_name, bank_domain)
            dedup: Dict[str, GmailCandidate] = {}
            for item in gmail_candidates + people_candidates:
                current = dedup.get(item.email)
                if current is None or item.score > current.score:
                    dedup[item.email] = item
            recipient_candidates = sorted(dedup.values(), key=lambda x: (-x.score, -x.message_count, x.email))
            st.session_state["recipient_candidates"] = [asdict(x) for x in recipient_candidates]
            st.session_state["active_scope_info"] = scope_info
            if CONTACTS_SCOPE in scope_info:
                st.success(f"Found {len(recipient_candidates)} likely recipients from Gmail and Contacts.")
            else:
                st.warning(
                    f"Found {len(recipient_candidates)} likely recipients from Gmail. Contacts search was skipped because the SQLite token does not include contacts.readonly scope."
                )
        except TokenRefreshError as exc:
            st.error(str(exc))
            st.info("Use the sidebar button 'Re-authorize selected Gmail token' to store a fresh Google token in the SQLite token DB.")
        except Exception as exc:
            st.error(f"Could not search Gmail / Contacts: {exc}")

    active_scopes = st.session_state.get("active_scope_info", [])
    if active_scopes:
        st.caption("Active token scopes: " + ", ".join(active_scopes))

    stored_candidates = [GmailCandidate(**x) for x in st.session_state.get("recipient_candidates", [])]
    if stored_candidates:
        st.dataframe(
            [
                {
                    "source": x.source,
                    "name": x.name,
                    "email": x.email,
                    "score": round(x.score, 2),
                    "messages": x.message_count,
                    "last_seen": x.last_seen,
                    "subjects": " | ".join(x.sample_subjects),
                }
                for x in stored_candidates
            ],
            use_container_width=True,
        )

    default_recipient = stored_candidates[0].email if stored_candidates else ""
    default_name = stored_candidates[0].name if stored_candidates else ""
    default_source = stored_candidates[0].source if stored_candidates else "manual"

    st.subheader("Recipient and draft")
    r1, r2 = st.columns([1.1, 0.9])
    with r1:
        recipient_name = st.text_input("Recipient name", value=default_name)
        recipient_email = st.text_input("Recipient email", value=default_recipient)
        recipient_source = st.text_input("Recipient source", value=default_source)
        cc_email = st.text_input("CC (optional)", value=complaint.user_email or "")
    with r2:
        st.caption("If Gmail does not find the right person, enter the official charge-back, card-services, or relationship-manager email manually.")

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
            if not recipient_email.strip():
                st.error("Recipient email is required.")
            else:
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
                    notes={"cc": cc_email, "token_key": st.session_state.token_key},
                )
                st.success("Saved draft record into bank_dispute_outreach.")
    with a2:
        if st.button("Create Gmail draft"):
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
                    notes={"cc": cc_email, "token_key": st.session_state.token_key},
                )
                st.success(f"Created Gmail draft: {draft_id}")
            except TokenRefreshError as exc:
                st.error(str(exc))
                st.info("Use the sidebar button 'Re-authorize selected Gmail token' and then try again.")
            except Exception as exc:
                st.error(f"Could not create Gmail draft: {exc}")
    with a3:
        if st.button("Send email now"):
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
                    notes={"cc": cc_email, "token_key": st.session_state.token_key},
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
