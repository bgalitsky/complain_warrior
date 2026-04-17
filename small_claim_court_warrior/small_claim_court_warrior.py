from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import textwrap
import time
import zipfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import streamlit as st

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from pypdf import PdfReader, PdfWriter
except Exception:  # pragma: no cover
    PdfReader = None
    PdfWriter = None

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
except Exception:  # pragma: no cover
    colors = None
    LETTER = None
    ParagraphStyle = None
    getSampleStyleSheet = None
    inch = None
    Paragraph = None
    SimpleDocTemplate = None
    Spacer = None
    Table = None
    TableStyle = None

APP_TITLE = "Small Claim Court Warrior"
APP_SUBTITLE = "Generate a California small-claims packet after Complaint Warrior has exhausted pre-suit options."

OFFICIAL_SC100_URL = "https://courts.ca.gov/sites/default/files/courts/default/2024-11/sc100.pdf"
SMALL_CLAIMS_HELP_URL = "https://selfhelp.courts.ca.gov/small-claims/start-case/forms/fill-out-forms"
SERVE_URL = "https://selfhelp.courts.ca.gov/small-claims/start-case/serve"
FILE_URL = "https://selfhelp.courts.ca.gov/small-claims/start-case/file"

DEFAULT_TABLE_SYNONYMS: dict[str, list[str]] = {
    "case_id": ["case_id", "id", "complaint_id", "ticket_id", "claim_id"],
    "company_name": [
        "company_name",
        "merchant_name",
        "business_name",
        "vendor_name",
        "respondent_name",
        "defendant_name",
        "company",
        "merchant",
    ],
    "amount": [
        "target_compensation_amount",
        "demand_amount",
        "amount_claimed",
        "amount_requested",
        "requested_amount",
        "settlement_amount",
        "compensation_amount",
        "amount",
        "claim_amount",
        "final_amount",
    ],
    "county": ["county", "filing_county", "venue_county", "court_county"],
    "status": ["status", "case_status", "resolution_status", "complaint_status"],
    "complaint_text": [
        "complaint_text",
        "complaint",
        "complaint_body",
        "consumer_complaint",
        "issue_summary",
        "incident_description",
        "claim_summary",
    ],
    "log_text": [
        "activity_log",
        "negotiation_log",
        "resolution_log",
        "outreach_log",
        "call_log",
        "email_thread",
        "event_log",
        "transcript",
        "log_text",
        "notes",
    ],
    "consumer_name": ["consumer_name", "customer_name", "plaintiff_name", "claimant_name", "user_name"],
    "consumer_email": ["consumer_email", "customer_email", "plaintiff_email", "email"],
    "consumer_phone": ["consumer_phone", "customer_phone", "plaintiff_phone", "phone"],
    "consumer_address": [
        "consumer_address",
        "customer_address",
        "plaintiff_address",
        "mailing_address",
        "address",
    ],
    "county_basis": ["county_basis", "venue_reason", "filing_reason"],
    "demand_date": ["demand_date", "last_demand_date", "final_demand_date"],
}

EXHAUSTED_KEYWORDS = [
    "exhausted",
    "closed_no_resolution",
    "final_offer_rejected",
    "escalation_failed",
    "pre_suit_complete",
    "ready_for_small_claims",
    "unresolved",
    "company_refused",
    "deadlock",
    "escalated",
    "denied",
    "rejected",
    "small claims",
]

COUNTY_OPTIONS = [
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa",
    "Del Norte", "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo",
    "Kern", "Kings", "Lake", "Lassen", "Los Angeles", "Madera", "Marin", "Mariposa",
    "Mendocino", "Merced", "Modoc", "Mono", "Monterey", "Napa", "Nevada", "Orange",
    "Placer", "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo", "San Mateo", "Santa Barbara",
    "Santa Clara", "Santa Cruz", "Shasta", "Sierra", "Siskiyou", "Solano", "Sonoma",
    "Stanislaus", "Sutter", "Tehama", "Trinity", "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
]

CA_CITY_TO_COUNTY = {
    "san jose": "Santa Clara",
    "fruitdale": "Santa Clara",
    "san francisco": "San Francisco",
    "oakland": "Alameda",
    "berkeley": "Alameda",
    "sacramento": "Sacramento",
    "san diego": "San Diego",
    "fresno": "Fresno",
    "san mateo": "San Mateo",
    "redwood city": "San Mateo",
    "palo alto": "Santa Clara",
    "cupertino": "Santa Clara",
    "sunnyvale": "Santa Clara",
    "santa clara": "Santa Clara",
    "los angeles": "Los Angeles",
    "long beach": "Los Angeles",
    "anaheim": "Orange",
    "irvine": "Orange",
    "santa ana": "Orange",
}

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "aol.com", "proton.me", "protonmail.com"
}


@dataclass
class CaseRecord:
    case_id: str
    company_name: str = ""
    amount: float = 0.0
    county: str = ""
    status: str = ""
    complaint_text: str = ""
    log_text: str = ""
    consumer_name: str = ""
    consumer_email: str = ""
    consumer_phone: str = ""
    consumer_address: str = ""
    county_basis: str = ""
    demand_date: str = ""
    raw_row: dict[str, Any] | None = None


@dataclass
class FilingPacket:
    sc100: dict[str, Any]
    damages_rows: list[dict[str, Any]]
    exhibit_rows: list[dict[str, Any]]
    timeline_rows: list[dict[str, Any]]
    claim_markdown: str
    workflow_markdown: str
    notes: list[str]


# -----------------------------
# Generic helpers
# -----------------------------

def money_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d.\-]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_address(text: str) -> tuple[str, str, str, str]:
    text = clean_text(text)
    if not text:
        return "", "", "CA", ""
    parts = [p.strip() for p in re.split(r"\n|,", text) if p.strip()]
    street = parts[0] if parts else ""
    city = ""
    state = "CA"
    postal = ""
    if len(parts) >= 2:
        city_state_zip = parts[-1]
        match = re.search(r"([A-Za-z .'-]+)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", city_state_zip)
        if match:
            city = match.group(1).strip()
            state = match.group(2).strip()
            postal = match.group(3).strip()
        else:
            city = city_state_zip
    return street, city, state, postal


def first_non_empty(values: Iterable[Any]) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def text_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def json_loads_safe(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def compact_filename(path_text: str) -> str:
    if not path_text:
        return ""
    return Path(path_text).name or path_text


# -----------------------------
# Complaint Warrior native parser
# -----------------------------

def is_complaint_warrior_db(conn: sqlite3.Connection) -> bool:
    tables = set(get_tables(conn))
    if "complaints" not in tables:
        return False
    complaint_cols = set(get_columns(conn, "complaints"))
    return {"complaint_id", "complaint_json"}.issubset(complaint_cols)


def load_call_results_map(conn: sqlite3.Connection) -> dict[str, list[dict[str, str]]]:
    if "call_results" not in get_tables(conn):
        return {}
    rows = conn.execute("SELECT call_sid, result_json FROM call_results ORDER BY updated_at").fetchall()
    by_case: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        call_sid = row[0]
        case_id = call_sid.split(":", 1)[0]
        payload = json_loads_safe(row[1])
        transcript = clean_text(payload.get("transcript"))
        if not transcript:
            continue
        by_case.setdefault(case_id, []).append({"call_sid": call_sid, "transcript": transcript})
    return by_case


def extract_emails(text: str) -> list[str]:
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    out: list[str] = []
    for email in found:
        email = email.lower()
        if email not in out:
            out.append(email)
    return out


def extract_phones(text: str) -> list[str]:
    found = re.findall(r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})", text or "")
    out: list[str] = []
    for phone in found:
        phone = re.sub(r"\s+", " ", phone).strip()
        if phone not in out:
            out.append(phone)
    return out


def email_domain_to_name(email: str) -> str:
    domain = email.split("@", 1)[-1].lower()
    if domain in PERSONAL_EMAIL_DOMAINS:
        return ""
    core = domain.split(".")[0].replace("-", " ").replace("_", " ").strip()
    return core.title()


def infer_company_name_from_case(obj: dict[str, Any]) -> str:
    complaint = clean_text(obj.get("complaint_raw"))
    subject = clean_text(obj.get("subject"))
    full_text = "\n".join(
        [
            complaint,
            clean_text(obj.get("complaint_professional")),
            clean_text(obj.get("final_conclusion")),
        ]
    )

    candidates: list[tuple[int, str]] = []

    patterns = [
        r"request that\s+([A-Z][A-Za-z&.'\- ]{2,80}?)\s+(?:ensure|address|review|provide|accept|correct|resolve)",
        r"appropriate contact is\s+([A-Z][A-Za-z&.'\- ]{2,80}?)(?=\s+at\s|\s+or\s|[.,]|$)",
        r"with\s+([A-Z][A-Za-z&.'\- ]{2,80}?)\s+ensure",
        r"technician\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2})(?=\s+on\b|\s+at\b|[.,]|$)",
        r"tax preparer,\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2})(?=\s*,|\s+whose\b|[.]|$)",
        r"store manager\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2})(?=\s+[A-Za-z0-9._%+-]+@|\s+\d{3}|[.,]|$)",
        r"piano instructor\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2})(?=\s*,|[.]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, full_text, flags=re.IGNORECASE):
            cand = clean_text(match.group(1)).strip("[]")
            if not cand:
                continue
            if "item name" in cand.lower():
                continue
            candidates.append((95, cand))

    # Ignore generic placeholders such as [Item Name] or [Music Academy Name].
    for email in extract_emails(full_text):
        inferred = email_domain_to_name(email)
        if inferred:
            candidates.append((99, inferred))

    if "ross" in full_text.lower():
        candidates.append((93, "Ross"))
    if "american airlines" in full_text.lower():
        candidates.append((93, "American Airlines"))
    if re.search(r"\bAA\d{3,5}\b", full_text):
        candidates.append((70, "American Airlines or airport operator"))
    if "carefirst home health" in full_text.lower():
        candidates.append((98, "CareFirst Home Health"))

    # Do not use the subject line as a company fallback because Complaint Warrior subjects are often issue summaries, not defendant names.

    if not candidates:
        return ""
    best = sorted(candidates, key=lambda x: (-x[0], len(x[1])))[0][1]
    return re.sub(r"\s{2,}", " ", best).strip(" .,-")


def extract_money_candidates(text: str, source_weight: int = 0) -> list[tuple[int, float, str]]:
    candidates: list[tuple[int, float, str]] = []
    for match in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)", text or ""):
        raw = match.group(0)
        amt = money_to_float(raw)
        if amt <= 0:
            continue
        start = max(0, match.start() - 70)
        end = min(len(text), match.end() + 70)
        ctx = text[start:end].lower()
        score = source_weight
        if "e.g." in ctx or "[amount" in ctx or "example" in ctx:
            score -= 6
        if "voucher number" in ctx:
            score -= 8
        if any(w in ctx for w in ["want", "request", "asking", "refund", "reimbursement", "back", "compensation", "rebooking fee", "paid", "cost me"]):
            score += 7
        if any(w in ctx for w in ["hotel", "meals", "flight", "repair", "damage", "deposit", "refund owed"]):
            score += 2
        if any(w in ctx for w in ["within two days", "offers", "issue compensation", "voucher value"]):
            score += 1
        candidates.append((score, amt, ctx))
    return candidates


def infer_amount_from_case(obj: dict[str, Any]) -> float:
    candidates: list[tuple[int, float, str]] = []
    candidates.extend(extract_money_candidates(clean_text(obj.get("complaint_raw")), 5))
    candidates.extend(extract_money_candidates(clean_text(obj.get("complaint_professional")), 3))
    candidates.extend(extract_money_candidates(clean_text(obj.get("final_conclusion")), 4))

    for activity in obj.get("activities", []):
        detail = clean_text(activity.get("detail"))
        kind = clean_text(activity.get("kind")).lower()
        channel = clean_text(activity.get("channel")).lower()
        extra_weight = 2 if kind == "received" else 0
        if channel == "phone":
            extra_weight += 1
        candidates.extend(extract_money_candidates(detail, 1 + extra_weight))

    for thread in obj.get("threads", {}).values():
        satisfaction = thread.get("satisfaction") or {}
        reason = clean_text(satisfaction.get("reason"))
        signals = satisfaction.get("signals") or {}
        response_excerpt = clean_text(signals.get("response_excerpt"))
        candidates.extend(extract_money_candidates(reason, 4))
        candidates.extend(extract_money_candidates(response_excerpt, 2))
        for phrase in signals.get("key_phrases", []) or []:
            candidates.extend(extract_money_candidates(clean_text(phrase), 3))

    if not candidates:
        return 0.0
    best = sorted(candidates, key=lambda x: (-x[0], -x[1]))[0]
    return float(best[1])


def infer_county_from_case(obj: dict[str, Any]) -> str:
    text = "\n".join(
        [
            clean_text(obj.get("complaint_raw")),
            clean_text(obj.get("complaint_professional")),
            clean_text(obj.get("final_conclusion")),
        ]
    ).lower()
    for city, county in CA_CITY_TO_COUNTY.items():
        if city in text:
            return county
    return ""


def infer_demand_date_from_case(obj: dict[str, Any]) -> str:
    activities = obj.get("activities", [])
    sent_dates: list[str] = []
    for activity in activities:
        if clean_text(activity.get("kind")).lower() == "sent":
            ts = clean_text(activity.get("ts"))
            if ts:
                sent_dates.append(ts.split("T", 1)[0])
    return sent_dates[-1] if sent_dates else clean_text(obj.get("created_at")).split("T", 1)[0]


def build_native_log_text(obj: dict[str, Any], call_map: dict[str, list[dict[str, str]]]) -> str:
    case_id = clean_text(obj.get("complaint_id"))
    blocks: list[str] = []
    status = clean_text(obj.get("current_status_summary"))
    final_conclusion = clean_text(obj.get("final_conclusion"))
    if status:
        blocks.append(f"STATUS: {status}")
    if final_conclusion:
        blocks.append(f"FINAL CONCLUSION: {final_conclusion}")

    for activity in obj.get("activities", []):
        ts = clean_text(activity.get("ts"))
        channel = clean_text(activity.get("channel")) or "system"
        kind = clean_text(activity.get("kind")) or "event"
        title = clean_text(activity.get("title"))
        detail = clean_text(activity.get("detail"))
        meta = activity.get("meta") or {}
        meta_bits: list[str] = []
        if isinstance(meta, dict):
            for k in ["stage", "verdict", "subject", "confidence"]:
                if meta.get(k) not in [None, "", []]:
                    meta_bits.append(f"{k}={meta.get(k)}")
        line = f"{ts} | {channel}/{kind} | {title}".strip(" |")
        if meta_bits:
            line += " | " + ", ".join(meta_bits)
        if detail:
            line += "\n" + detail
        blocks.append(line)

    if case_id in call_map:
        for item in call_map[case_id]:
            blocks.append(f"PHONE TRANSCRIPT | {item['call_sid']}\n{item['transcript']}")

    return "\n\n".join([b for b in blocks if b.strip()])


def build_native_case_records(conn: sqlite3.Connection) -> list[CaseRecord]:
    call_map = load_call_results_map(conn)
    rows = conn.execute("SELECT user_email, complaint_id, complaint_json, updated_at FROM complaints ORDER BY updated_at DESC").fetchall()
    records: list[CaseRecord] = []
    for user_email, complaint_id, complaint_json, _updated_at in rows:
        obj = json_loads_safe(complaint_json)
        complaint_text = first_non_empty([obj.get("complaint_raw"), obj.get("complaint_professional")])
        log_text = build_native_log_text(obj, call_map)
        company_name = infer_company_name_from_case(obj)
        amount = infer_amount_from_case(obj)
        county = infer_county_from_case(obj)
        demand_date = infer_demand_date_from_case(obj)
        consumer_phone = ""
        phone_hits = extract_phones(complaint_text)
        if phone_hits:
            consumer_phone = phone_hits[0]

        record = CaseRecord(
            case_id=complaint_id or clean_text(obj.get("complaint_id")) or "",
            company_name=company_name,
            amount=amount,
            county=county,
            status=clean_text(obj.get("current_status_summary")),
            complaint_text=complaint_text,
            log_text=log_text,
            consumer_name=clean_text(obj.get("user_name")),
            consumer_email=clean_text(obj.get("user_email")) or user_email,
            consumer_phone=consumer_phone,
            consumer_address="",
            county_basis="",
            demand_date=demand_date,
            raw_row=obj,
        )
        records.append(record)
    return records


def complaint_warrior_preview_df(records: list[CaseRecord]) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append(
            {
                "complaint_id": r.case_id,
                "company_name": r.company_name,
                "amount": r.amount,
                "county": r.county,
                "status": r.status,
                "consumer_name": r.consumer_name,
                "consumer_email": r.consumer_email,
                "demand_date": r.demand_date,
            }
        )
    return pd.DataFrame(rows)


# -----------------------------
# SQLite discovery and mapping
# -----------------------------

DB_CANDIDATE_NAMES = [
    "cw_store.sqlite",
    "cw_store.db",
    "cw_store.sqlite3",
    "cw_store(1).sqlite",
    "cw_store(1).db",
    "cw_store(1).sqlite3",
]


def discover_default_db_path() -> Optional[Path]:
    search_dirs: list[Path] = []
    cwd = Path.cwd().resolve()
    search_dirs.append(cwd)
    try:
        script_dir = Path(__file__).resolve().parent
        if script_dir not in search_dirs:
            search_dirs.append(script_dir)
    except Exception:
        pass

    for directory in search_dirs:
        for name in DB_CANDIDATE_NAMES:
            candidate = directory / name
            if candidate.exists() and candidate.is_file():
                return candidate

    discovered: list[Path] = []
    for directory in search_dirs:
        for pattern in ("*.sqlite", "*.db", "*.sqlite3"):
            for candidate in directory.glob(pattern):
                if candidate.is_file():
                    discovered.append(candidate.resolve())
    if not discovered:
        return None

    def score(p: Path) -> tuple[int, float]:
        name = p.name.lower()
        priority = 0
        if "cw_store" in name:
            priority += 10
        if "complaint" in name:
            priority += 3
        return priority, p.stat().st_mtime

    discovered = sorted({p for p in discovered}, key=score, reverse=True)
    return discovered[0]


def open_sqlite_connection(db_bytes: bytes | None, db_path: str | None) -> tuple[sqlite3.Connection, Optional[str]]:
    if db_bytes:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        temp.write(db_bytes)
        temp.flush()
        temp.close()
        conn = sqlite3.connect(temp.name)
        conn.row_factory = sqlite3.Row
        return conn, temp.name
    if db_path:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn, None
    raise ValueError("Provide either uploaded database bytes or a database path.")


def get_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [row[1] for row in rows]

def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def ensure_small_claim_packets_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS small_claim_packets (
            packet_id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id TEXT NOT NULL,
            is_latest INTEGER NOT NULL DEFAULT 1,
            saved_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            plaintiff_name TEXT,
            defendant_legal_name TEXT,
            company_name TEXT,
            amount REAL,
            county TEXT,
            status TEXT,
            packet_json TEXT NOT NULL,
            notes_json TEXT,
            claim_markdown TEXT,
            workflow_markdown TEXT,
            damages_csv BLOB,
            exhibits_csv BLOB,
            timeline_csv BLOB,
            packet_zip BLOB,
            packet_pdf BLOB,
            filled_sc100_pdf BLOB
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_small_claim_packets_case_latest ON small_claim_packets(complaint_id, is_latest, saved_at DESC)"
    )
    conn.commit()


def assemble_address(street: str, city: str, state: str, postal: str) -> str:
    street = clean_text(street)
    city = clean_text(city)
    state = clean_text(state)
    postal = clean_text(postal)
    parts = []
    if street:
        parts.append(street)
    city_line = " ".join([x for x in [f"{city}," if city and (state or postal) else city, state, postal] if x]).strip()
    city_line = city_line.replace(' ,', ',')
    if city_line:
        parts.append(city_line)
    return "\n".join(parts).strip()


def load_latest_saved_packet(conn: sqlite3.Connection, complaint_id: str) -> Optional[dict[str, Any]]:
    if not complaint_id or not table_exists(conn, 'small_claim_packets'):
        return None
    row = conn.execute(
        "SELECT * FROM small_claim_packets WHERE complaint_id=? ORDER BY is_latest DESC, saved_at DESC LIMIT 1",
        (complaint_id,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data['packet_json_parsed'] = json_loads_safe(data.get('packet_json'))
    try:
        data['notes_json_parsed'] = json.loads(data.get('notes_json') or '[]')
    except Exception:
        data['notes_json_parsed'] = []
    return data


def save_packet_to_db(
    conn: sqlite3.Connection,
    packet: FilingPacket,
    packet_pdf: bytes | None = None,
    filled_sc100_pdf: bytes | None = None,
) -> dict[str, Any]:
    ensure_small_claim_packets_table(conn)
    damages_df = pd.DataFrame(packet.damages_rows)
    exhibits_df = pd.DataFrame(packet.exhibit_rows)
    timeline_df = pd.DataFrame(packet.timeline_rows)
    packet_zip = build_download_zip(packet=packet, filled_pdf=filled_sc100_pdf, packet_pdf=packet_pdf)
    now = time.time()
    complaint_id = clean_text(packet.sc100.get('case_id'))

    conn.execute(
        "UPDATE small_claim_packets SET is_latest=0, updated_at=? WHERE complaint_id=? AND is_latest=1",
        (now, complaint_id),
    )
    cursor = conn.execute(
        """
        INSERT INTO small_claim_packets (
            complaint_id, is_latest, saved_at, updated_at,
            plaintiff_name, defendant_legal_name, company_name, amount, county, status,
            packet_json, notes_json, claim_markdown, workflow_markdown,
            damages_csv, exhibits_csv, timeline_csv, packet_zip, packet_pdf, filled_sc100_pdf
        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            complaint_id,
            now,
            now,
            clean_text(packet.sc100.get('plaintiff_name')),
            clean_text(packet.sc100.get('defendant_legal_name')),
            clean_text(packet.sc100.get('company_name')),
            float(packet.sc100.get('amount') or 0.0),
            clean_text(packet.sc100.get('county')),
            clean_text(packet.sc100.get('status')),
            json.dumps(packet.sc100, ensure_ascii=False),
            json.dumps(packet.notes, ensure_ascii=False),
            packet.claim_markdown,
            packet.workflow_markdown,
            dataframe_to_csv_bytes(damages_df),
            dataframe_to_csv_bytes(exhibits_df),
            dataframe_to_csv_bytes(timeline_df),
            packet_zip,
            packet_pdf,
            filled_sc100_pdf,
        ),
    )
    conn.commit()
    return {
        'packet_id': cursor.lastrowid,
        'complaint_id': complaint_id,
        'saved_at': now,
        'packet_zip': packet_zip,
    }


def score_table(columns: Sequence[str]) -> int:
    normalized = {normalize_key(c) for c in columns}
    score = 0
    for synonyms in DEFAULT_TABLE_SYNONYMS.values():
        for synonym in synonyms:
            if normalize_key(synonym) in normalized:
                score += 1
    for key in ["company", "complaint", "amount", "county", "status", "log"]:
        if any(key in c for c in normalized):
            score += 1
    return score


def auto_detect_table(conn: sqlite3.Connection) -> Optional[str]:
    best_table = None
    best_score = -1
    for table in get_tables(conn):
        cols = get_columns(conn, table)
        score = score_table(cols)
        if score > best_score:
            best_table = table
            best_score = score
    return best_table


def auto_detect_columns(columns: Sequence[str]) -> dict[str, Optional[str]]:
    normalized_to_original = {normalize_key(c): c for c in columns}
    chosen: dict[str, Optional[str]] = {}
    for logical_name, synonyms in DEFAULT_TABLE_SYNONYMS.items():
        chosen[logical_name] = None
        for synonym in synonyms:
            hit = normalized_to_original.get(normalize_key(synonym))
            if hit:
                chosen[logical_name] = hit
                break
    return chosen


def read_table(conn: sqlite3.Connection, table: str, limit: int = 500) -> pd.DataFrame:
    query = f"SELECT * FROM '{table}' LIMIT {int(limit)}"
    return pd.read_sql_query(query, conn)


def build_case_records(df: pd.DataFrame, mapping: dict[str, Optional[str]]) -> list[CaseRecord]:
    records: list[CaseRecord] = []
    for _, row in df.iterrows():
        as_dict = row.to_dict()
        case_id = first_non_empty([
            as_dict.get(mapping.get("case_id") or "", ""),
            as_dict.get("id", ""),
        ])
        if not case_id:
            case_id = str(len(records) + 1)

        record = CaseRecord(
            case_id=str(case_id),
            company_name=first_non_empty([as_dict.get(mapping.get("company_name") or "")]),
            amount=money_to_float(as_dict.get(mapping.get("amount") or "")),
            county=first_non_empty([as_dict.get(mapping.get("county") or "")]),
            status=first_non_empty([as_dict.get(mapping.get("status") or "")]),
            complaint_text=clean_text(as_dict.get(mapping.get("complaint_text") or "")),
            log_text=clean_text(as_dict.get(mapping.get("log_text") or "")),
            consumer_name=first_non_empty([as_dict.get(mapping.get("consumer_name") or "")]),
            consumer_email=first_non_empty([as_dict.get(mapping.get("consumer_email") or "")]),
            consumer_phone=first_non_empty([as_dict.get(mapping.get("consumer_phone") or "")]),
            consumer_address=first_non_empty([as_dict.get(mapping.get("consumer_address") or "")]),
            county_basis=first_non_empty([as_dict.get(mapping.get("county_basis") or "")]),
            demand_date=first_non_empty([as_dict.get(mapping.get("demand_date") or "")]),
            raw_row=as_dict,
        )
        records.append(record)
    return records


def record_is_exhausted(record: CaseRecord) -> bool:
    status_text = clean_text(record.status).lower()
    if any(x in status_text for x in ["resolved", "satisfy the demand"]):
        return False
    if any(x in status_text for x in ["escalated", "denied", "rejected", "deadlock", "ready for small claims", "closed no resolution"]):
        return True

    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    for thread in (raw.get("threads") or {}).values():
        stage = clean_text(thread.get("stage")).lower()
        tstatus = clean_text(thread.get("status")).lower()
        if stage == "resolved" or tstatus == "resolved":
            return False
        if stage == "escalated":
            return True
        decision = thread.get("last_decision") or {}
        if clean_text(decision.get("action")).lower() == "escalate":
            return True

    haystack = " ".join([record.status, record.log_text[-2000:]]).lower()
    filtered_keywords = [kw for kw in EXHAUSTED_KEYWORDS if kw != "unresolved"]
    exhausted = any(keyword in haystack for keyword in filtered_keywords)
    return exhausted


# -----------------------------
# Packet generation
# -----------------------------

def infer_short_reason(complaint_text: str, log_text: str, company_name: str) -> str:
    source = first_non_empty([complaint_text, log_text])
    if not source:
        return f"Consumer dispute against {company_name or 'the company'} after unsuccessful efforts to obtain compensation."
    sentence = re.split(r"(?<=[.!?])\s+", source)[0].strip()
    if len(sentence) > 280:
        sentence = sentence[:277].rstrip() + "..."
    return sentence


def infer_county_basis(county: str, existing_basis: str, company_name: str) -> str:
    if existing_basis:
        return existing_basis
    county = county.strip()
    if county:
        return (
            f"This claim is filed in {county} County because the transaction, communications, "
            f"or resulting harm occurred there, or the defendant does business there."
        )
    return (
        f"Complaint Warrior did not store an explicit filing county for this case. "
        f"Confirm where {company_name or 'the defendant'} does business, where the transaction occurred, or where the harm occurred."
    )


def infer_timeline_rows(complaint_text: str, log_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    date_pattern = re.compile(
        r"(?P<date>(?:\d{1,2}/\d{1,2}/\d{2,4})|(?:\d{4}-\d{2}-\d{2})|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s*\d{4})",
        re.IGNORECASE,
    )

    for idx, line in enumerate([ln for ln in log_text.splitlines() if ln.strip()], start=1):
        match = date_pattern.search(line)
        if match:
            when = match.group("date")
            event = line.replace(when, "").strip(" :-|")
            rows.append({"seq": idx, "date": when, "event": event or line})
        elif line.startswith("20") and "|" in line:
            ts, rest = line.split("|", 1)
            rows.append({"seq": idx, "date": ts.strip().split("T", 1)[0], "event": rest.strip()})
        elif len(rows) < 10:
            rows.append({"seq": idx, "date": "", "event": line.strip(" -*\t")})
        if len(rows) >= 14:
            break

    if not rows:
        lines = [line.strip(" -*\t") for line in complaint_text.splitlines() if line.strip()]
        for idx, line in enumerate(lines[:6], start=1):
            rows.append({"seq": idx, "date": "", "event": line})
    return rows


def infer_exhibit_rows(record: CaseRecord, amount: float) -> list[dict[str, str]]:
    base_rows = [
        {"exhibit": "A", "description": "Original written complaint or dispute summary", "source": "Complaint Warrior complaint_raw"},
        {"exhibit": "B", "description": "Negotiation, outreach, and status log", "source": "Complaint Warrior activities and call results"},
        {"exhibit": "C", "description": f"Damages worksheet supporting claimed amount of {fmt_money(amount)}", "source": "Generated by Small Claim Court Warrior"},
    ]

    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    docs = raw.get("docs") or []
    evidence_pack = clean_text(raw.get("evidence_pack_pdf"))
    label_ord = ord("D")

    if evidence_pack:
        base_rows.append(
            {
                "exhibit": chr(label_ord),
                "description": f"Complaint Warrior evidence pack ({compact_filename(evidence_pack)})",
                "source": evidence_pack,
            }
        )
        label_ord += 1

    for doc in docs[:8]:
        base_rows.append(
            {
                "exhibit": chr(label_ord),
                "description": f"Supporting document: {compact_filename(str(doc))}",
                "source": str(doc),
            }
        )
        label_ord += 1

    haystack = f"{record.complaint_text}\n{record.log_text}".lower()
    extras = {
        "email": "Email thread or messages with the company",
        "call": "Phone transcript or call notes",
        "photo": "Photos or screenshots showing the problem",
        "receipt": "Receipts, invoices, or payment proof",
        "warranty": "Warranty, policy, or terms excerpt",
        "refund": "Refund denial or final offer from company",
    }
    for marker, desc in extras.items():
        if marker in haystack and len(base_rows) < 12:
            base_rows.append({"exhibit": chr(label_ord), "description": desc, "source": "Inferred from case text"})
            label_ord += 1
    return base_rows


def infer_damages_rows(amount: float, complaint_text: str) -> list[dict[str, Any]]:
    lower = complaint_text.lower()
    rows: list[dict[str, Any]] = []

    if amount <= 0:
        return [
            {
                "line": 1,
                "category": "Amount to confirm",
                "basis": "Complaint Warrior did not store a reliable demand amount for this case",
                "amount": 0.0,
            }
        ]

    if "hotel" in lower or "meal" in lower:
        rows.append({"line": len(rows) + 1, "category": "Travel disruption costs", "basis": "Hotel, meal, or rebooking costs described in complaint", "amount": amount})
    elif any(word in lower for word in ["deposit", "security deposit"]):
        rows.append({"line": len(rows) + 1, "category": "Deposit wrongfully withheld", "basis": "Complaint description", "amount": amount})
    elif any(word in lower for word in ["refund", "return", "back which i paid"]):
        rows.append({"line": len(rows) + 1, "category": "Refund owed", "basis": "Consumer refund demand", "amount": amount})
    elif any(word in lower for word in ["repair", "damage", "broken"]):
        rows.append({"line": len(rows) + 1, "category": "Repair, replacement, or property damage", "basis": "Complaint description", "amount": amount})
    else:
        rows.append({"line": len(rows) + 1, "category": "Requested compensation", "basis": "Complaint Warrior demand / user-confirmed amount", "amount": amount})

    return rows


def build_sc100_payload(
    record: CaseRecord,
    plaintiff_name: str,
    plaintiff_address: str,
    plaintiff_email: str,
    plaintiff_phone: str,
    defendant_legal_name: str,
    defendant_address: str,
    agent_for_service: str,
    demand_date: str,
    county_basis: str,
    amount: float,
    county: str,
    claim_summary: str,
    claim_detail: str,
    damages_summary: str,
) -> dict[str, Any]:
    p_street, p_city, p_state, p_zip = split_address(plaintiff_address)
    d_street, d_city, d_state, d_zip = split_address(defendant_address)
    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    return {
        "plaintiff_name": plaintiff_name,
        "plaintiff_street": p_street,
        "plaintiff_city": p_city,
        "plaintiff_state": p_state,
        "plaintiff_zip": p_zip,
        "plaintiff_email": plaintiff_email,
        "plaintiff_phone": plaintiff_phone,
        "defendant_legal_name": defendant_legal_name,
        "defendant_street": d_street,
        "defendant_city": d_city,
        "defendant_state": d_state,
        "defendant_zip": d_zip,
        "agent_for_service": agent_for_service,
        "company_name": record.company_name,
        "amount": amount,
        "amount_text": fmt_money(amount),
        "county": county,
        "county_basis": county_basis,
        "demand_date": demand_date,
        "claim_summary": claim_summary,
        "claim_detail": claim_detail,
        "damages_summary": damages_summary,
        "complaint_text": record.complaint_text,
        "log_text": record.log_text,
        "case_id": record.case_id,
        "status": record.status,
        "cw_subject": clean_text(raw.get("subject")),
        "cw_final_conclusion": clean_text(raw.get("final_conclusion")),
    }


def render_claim_markdown(sc100: dict[str, Any], notes: list[str]) -> str:
    lines = [
        f"# SC-100 Draft for Case {sc100['case_id']}",
        "",
        "## Plaintiff",
        f"- Name: {sc100['plaintiff_name']}",
        f"- Address: {sc100['plaintiff_street']}, {sc100['plaintiff_city']}, {sc100['plaintiff_state']} {sc100['plaintiff_zip']}",
        f"- Phone: {sc100['plaintiff_phone']}",
        f"- Email: {sc100['plaintiff_email']}",
        "",
        "## Defendant",
        f"- Legal name: {sc100['defendant_legal_name']}",
        f"- Service address: {sc100['defendant_street']}, {sc100['defendant_city']}, {sc100['defendant_state']} {sc100['defendant_zip']}",
        f"- Agent for service: {sc100['agent_for_service'] or 'To be confirmed'}",
        "",
        "## Claim",
        f"- Amount demanded: {sc100['amount_text']}",
        f"- County: {sc100['county'] or 'To be confirmed'}",
        f"- Demand date: {sc100['demand_date'] or 'To be confirmed'}",
        f"- Short claim statement: {sc100['claim_summary']}",
        "",
        "## Why this county",
        sc100["county_basis"],
        "",
        "## Detailed claim statement",
        sc100["claim_detail"],
        "",
        "## Notes before filing",
    ]
    lines.extend([f"- {note}" for note in notes])
    lines.append("")
    return "\n".join(lines)


def render_workflow_markdown(sc100: dict[str, Any]) -> str:
    amount = sc100["amount"]
    if amount <= 1500:
        fee = "$30"
    elif amount <= 5000:
        fee = "$50"
    elif amount <= 12500:
        fee = "$75"
    else:
        fee = "$100 or outside the usual individual small-claims limit"

    return textwrap.dedent(
        f"""
        # Human-in-the-loop filing workflow

        1. **Review the case facts**
           - Confirm the plaintiff name, address, defendant legal name, and service address.
           - Confirm the amount claimed: {sc100['amount_text']}.
           - Confirm the filing county: {sc100['county'] or 'To be confirmed'}.

        2. **Check the official court form**
           - Review the generated SC-100 draft against the latest California Judicial Council SC-100 form.
           - If using the uploaded blank PDF, inspect every populated field before filing.

        3. **Check filing fee and filing method**
           - Estimated statewide filing fee based on amount: {fee}.
           - Verify whether the selected county accepts small-claims e-filing, mail filing, or in-person filing.

        4. **Finalize evidence**
           - Attach the damages worksheet, timeline, and exhibit list.
           - Add receipts, screenshots, contracts, warranty terms, and final demand communications.

        5. **File the case**
           - File SC-100 and any needed attachment such as SC-100A.
           - Pay the filing fee or submit a fee-waiver request if needed.

        6. **Serve the defendant**
           - Do not serve the papers yourself.
           - Use a process server, sheriff, clerk service, or another adult who is not a party.
           - File proof of service after service is complete.

        7. **Prepare for the hearing**
           - Bring copies for yourself, the judge, and the defendant.
           - Bring a concise timeline and a 2-minute opening statement.
           - Bring any witnesses or subpoenas if needed.

        Official references:
        - Forms: {SMALL_CLAIMS_HELP_URL}
        - Filing fees and filing: {FILE_URL}
        - Service: {SERVE_URL}
        """
    ).strip()


def build_packet(
    record: CaseRecord,
    plaintiff_name: str,
    plaintiff_address: str,
    plaintiff_email: str,
    plaintiff_phone: str,
    defendant_legal_name: str,
    defendant_address: str,
    agent_for_service: str,
    demand_date: str,
    county_basis: str,
    amount: float,
    county: str,
    claim_summary: str,
    claim_detail: str,
) -> FilingPacket:
    damages_rows = infer_damages_rows(amount, record.complaint_text)
    exhibit_rows = infer_exhibit_rows(record, amount)
    timeline_rows = infer_timeline_rows(record.complaint_text, record.log_text)
    damages_summary = "; ".join([f"{row['category']}: {fmt_money(float(row['amount']))}" for row in damages_rows])

    sc100 = build_sc100_payload(
        record=record,
        plaintiff_name=plaintiff_name,
        plaintiff_address=plaintiff_address,
        plaintiff_email=plaintiff_email,
        plaintiff_phone=plaintiff_phone,
        defendant_legal_name=defendant_legal_name,
        defendant_address=defendant_address,
        agent_for_service=agent_for_service,
        demand_date=demand_date,
        county_basis=county_basis,
        amount=amount,
        county=county,
        claim_summary=claim_summary,
        claim_detail=claim_detail,
        damages_summary=damages_summary,
    )

    notes = []
    if not defendant_legal_name.strip():
        notes.append("Confirm the defendant's exact legal name before filing.")
    if not defendant_address.strip():
        notes.append("Confirm a physical service address and, if applicable, the agent for service of process.")
    if not demand_date.strip():
        notes.append("Record when the final demand for payment was made.")
    if amount <= 0:
        notes.append("Complaint Warrior did not store a reliable demand amount; confirm the amount before filing.")
    if amount > 12500:
        notes.append("Amount exceeds the usual California small-claims limit for a natural person; review before filing.")
    if not county.strip():
        notes.append("Complaint Warrior did not store venue data for this case. Confirm the correct county before filing.")
    if not notes:
        notes.append("Review and approve all facts before filing with the court.")

    claim_markdown = render_claim_markdown(sc100, notes)
    workflow_markdown = render_workflow_markdown(sc100)

    return FilingPacket(
        sc100=sc100,
        damages_rows=damages_rows,
        exhibit_rows=exhibit_rows,
        timeline_rows=timeline_rows,
        claim_markdown=claim_markdown,
        workflow_markdown=workflow_markdown,
        notes=notes,
    )


# -----------------------------
# PDF helpers (optional)
# -----------------------------

def fetch_official_sc100_pdf() -> bytes:
    if requests is None:
        raise RuntimeError("The requests package is not available.")
    response = requests.get(OFFICIAL_SC100_URL, timeout=30)
    response.raise_for_status()
    return response.content


def extract_pdf_fields(pdf_bytes: bytes) -> list[str]:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed.")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    fields = reader.get_fields() or {}
    return list(fields.keys())


def default_field_map(field_names: Sequence[str], sc100: dict[str, Any]) -> dict[str, str]:
    target_values = {
        "plaintiff_name": sc100.get("plaintiff_name", ""),
        "plaintiff_address": ", ".join([x for x in [sc100.get("plaintiff_street"), sc100.get("plaintiff_city"), sc100.get("plaintiff_state"), sc100.get("plaintiff_zip")] if x]),
        "plaintiff_phone": sc100.get("plaintiff_phone", ""),
        "plaintiff_email": sc100.get("plaintiff_email", ""),
        "defendant_name": sc100.get("defendant_legal_name", ""),
        "defendant_address": ", ".join([x for x in [sc100.get("defendant_street"), sc100.get("defendant_city"), sc100.get("defendant_state"), sc100.get("defendant_zip")] if x]),
        "agent_for_service": sc100.get("agent_for_service", ""),
        "amount": sc100.get("amount_text", ""),
        "county": sc100.get("county", ""),
        "county_basis": sc100.get("county_basis", ""),
        "claim_summary": sc100.get("claim_summary", ""),
        "claim_detail": sc100.get("claim_detail", ""),
        "demand_date": sc100.get("demand_date", ""),
    }

    matchers = {
        "plaintiff_name": ["plaintiff", "name"],
        "plaintiff_address": ["plaintiff", "address"],
        "plaintiff_phone": ["plaintiff", "phone"],
        "plaintiff_email": ["plaintiff", "email"],
        "defendant_name": ["defendant", "name"],
        "defendant_address": ["defendant", "address"],
        "agent_for_service": ["agent", "service"],
        "amount": ["amount"],
        "county": ["county"],
        "county_basis": ["why", "county"],
        "claim_summary": ["why", "claim"],
        "claim_detail": ["describe", "claim"],
        "demand_date": ["when", "asked"],
    }

    mapping: dict[str, str] = {}
    for field_name in field_names:
        nk = normalize_key(field_name)
        chosen_value = ""
        for target, words in matchers.items():
            if all(normalize_key(w) in nk for w in words):
                chosen_value = target_values[target]
                break
            if normalize_key(words[0]) in nk:
                chosen_value = target_values[target]
        mapping[field_name] = chosen_value
    return mapping


def fill_pdf_form(pdf_bytes: bytes, field_value_map: dict[str, str]) -> bytes:
    if PdfReader is None or PdfWriter is None:
        raise RuntimeError("pypdf is not installed.")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, field_value_map)
        except Exception:
            continue

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_packet_pdf(packet: FilingPacket) -> bytes:
    if SimpleDocTemplate is None:
        raise RuntimeError("reportlab is not installed.")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title="Small Claim Court Warrior Packet",
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        spaceAfter=8,
    )
    heading = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#1F3A5F"),
        spaceAfter=8,
        spaceBefore=10,
    )
    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#1F3A5F"),
        spaceAfter=12,
    )

    story = [
        Paragraph(APP_TITLE, title),
        Paragraph("Court-ready draft packet generated from Complaint Warrior data.", body),
        Spacer(1, 0.1 * inch),
        Paragraph("SC-100 Draft", heading),
    ]

    for line in packet.claim_markdown.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("## "):
            story.append(Paragraph(line.replace("## ", ""), heading))
        elif line.startswith("- "):
            story.append(Paragraph("&bull; " + line[2:], body))
        elif line.strip():
            story.append(Paragraph(line, body))
        else:
            story.append(Spacer(1, 0.05 * inch))

    story.extend([Spacer(1, 0.1 * inch), Paragraph("Damages Table", heading)])
    dmg_df = pd.DataFrame(packet.damages_rows)
    damage_table = Table([dmg_df.columns.tolist()] + dmg_df.astype(str).values.tolist(), repeatRows=1)
    damage_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F2")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEADING", (0, 0), (-1, -1), 12),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B0BFD1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(damage_table)

    story.extend([Spacer(1, 0.14 * inch), Paragraph("Exhibit List", heading)])
    exh_df = pd.DataFrame(packet.exhibit_rows)
    exhibit_table = Table([exh_df.columns.tolist()] + exh_df.astype(str).values.tolist(), repeatRows=1, colWidths=[0.7 * inch, 3.8 * inch, 2.1 * inch])
    exhibit_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F2")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B0BFD1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(exhibit_table)

    story.extend([Spacer(1, 0.14 * inch), Paragraph("Filing Workflow", heading)])
    for line in packet.workflow_markdown.splitlines():
        if re.match(r"^\d+\.\s", line):
            story.append(Paragraph(f"<b>{line}</b>", body))
        elif line.strip().startswith("-"):
            story.append(Paragraph("&bull; " + line.strip()[1:].strip(), body))
        elif line.strip():
            story.append(Paragraph(line.strip(), body))
        else:
            story.append(Spacer(1, 0.05 * inch))

    doc.build(story)
    return buffer.getvalue()


def build_download_zip(packet: FilingPacket, filled_pdf: bytes | None = None, packet_pdf: bytes | None = None) -> bytes:
    damages_df = pd.DataFrame(packet.damages_rows)
    exhibits_df = pd.DataFrame(packet.exhibit_rows)
    timeline_df = pd.DataFrame(packet.timeline_rows)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sc100_draft.md", text_bytes(packet.claim_markdown))
        zf.writestr("sc100_draft.json", json_bytes(packet.sc100))
        zf.writestr("damages_table.csv", dataframe_to_csv_bytes(damages_df))
        zf.writestr("exhibit_list.csv", dataframe_to_csv_bytes(exhibits_df))
        zf.writestr("timeline.csv", dataframe_to_csv_bytes(timeline_df))
        zf.writestr("filing_workflow.md", text_bytes(packet.workflow_markdown))
        if filled_pdf:
            zf.writestr("sc100_filled_best_effort.pdf", filled_pdf)
        if packet_pdf:
            zf.writestr("small_claim_packet.pdf", packet_pdf)
    return zip_buffer.getvalue()


# -----------------------------
# Streamlit UI
# -----------------------------

def init_page() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(APP_SUBTITLE)



def connection_ui() -> tuple[Optional[sqlite3.Connection], Optional[str]]:
    st.sidebar.header("Complaint Warrior database")
    auto_db_path = discover_default_db_path()

    if auto_db_path is not None:
        st.sidebar.success(f"Using local Complaint Warrior DB: {auto_db_path.name}")
        st.sidebar.caption(f"Auto-discovered in: {auto_db_path.parent}")
        try:
            conn, temp_path = open_sqlite_connection(None, str(auto_db_path))
            return conn, temp_path
        except Exception as exc:
            st.sidebar.error(f"Found {auto_db_path.name}, but could not open it: {exc}")

    st.sidebar.warning("No local Complaint Warrior DB was auto-detected in the current directory.")
    with st.sidebar.expander("Fallback connection options", expanded=True):
        uploaded = st.file_uploader("Upload Complaint Warrior .db file", type=["db", "sqlite", "sqlite3"], key="cw_db_upload")
        db_path = st.text_input("Optional SQLite path override", value=st.session_state.get("cw_db_path", ""))
        st.session_state["cw_db_path"] = db_path

    try:
        if uploaded is None and not db_path:
            return None, None
        conn, temp_path = open_sqlite_connection(uploaded.getvalue() if uploaded else None, db_path or None)
        st.sidebar.success("Database connected")
        return conn, temp_path
    except Exception as exc:
        st.sidebar.error(f"Could not open database: {exc}")
        return None, None



def native_case_browser_ui(conn: sqlite3.Connection) -> tuple[list[CaseRecord], Optional[CaseRecord]]:
    records = build_native_case_records(conn)
    st.subheader("1) Complaint Warrior cases")
    st.caption("Native Complaint Warrior schema detected. This app now reads JSON cases from the complaints table and merges phone transcripts from call_results.")
    preview_df = complaint_warrior_preview_df(records)
    st.dataframe(preview_df, use_container_width=True, hide_index=True)

    only_ready = st.checkbox("Show only cases likely ready for small claims", value=True)
    visible = [r for r in records if record_is_exhausted(r)] if only_ready else records
    if not visible:
        st.info("No cases were clearly marked as exhausted. Showing all Complaint Warrior cases instead.")
        visible = records

    labels = [
        f"{r.case_id} | {r.company_name or 'Review defendant name'} | {fmt_money(r.amount)} | {r.status or 'No status'}"
        for r in visible
    ]
    selected_label = st.selectbox("2) Choose Complaint Warrior case", labels)
    return records, visible[labels.index(selected_label)]



def select_mapping_ui(conn: sqlite3.Connection) -> tuple[Optional[str], dict[str, Optional[str]], Optional[pd.DataFrame]]:
    tables = get_tables(conn)
    if not tables:
        st.error("No tables found in the database.")
        return None, {}, None

    guessed_table = auto_detect_table(conn) or tables[0]
    st.subheader("1) Map database data")
    table = st.selectbox("Case table", tables, index=tables.index(guessed_table) if guessed_table in tables else 0)
    columns = get_columns(conn, table)
    auto_mapping = auto_detect_columns(columns)

    with st.expander("Column mapping", expanded=True):
        mapping: dict[str, Optional[str]] = {}
        for logical_name in DEFAULT_TABLE_SYNONYMS:
            choices = [None] + columns
            default = auto_mapping.get(logical_name)
            mapping[logical_name] = st.selectbox(
                logical_name.replace("_", " ").title(),
                choices,
                index=choices.index(default) if default in choices else 0,
                key=f"map_{logical_name}",
            )

    preview_df = read_table(conn, table, limit=500)
    st.dataframe(preview_df.head(25), use_container_width=True, hide_index=True)
    return table, mapping, preview_df



def select_case_ui(records: list[CaseRecord]) -> Optional[CaseRecord]:
    if not records:
        st.warning("No rows were loaded from the selected table.")
        return None

    only_exhausted = st.checkbox("Show only exhausted / unresolved cases", value=True)
    visible = [r for r in records if record_is_exhausted(r)] if only_exhausted else records
    if not visible:
        st.info("No rows matched the exhausted-case filter. Showing all rows instead.")
        visible = records

    labels = [
        f"{r.case_id} | {r.company_name or 'Unknown company'} | {fmt_money(r.amount)} | {r.county or 'No county'} | {r.status or 'No status'}"
        for r in visible
    ]
    selected_label = st.selectbox("2) Choose case", labels)
    return visible[labels.index(selected_label)]



def extracted_contacts_ui(record: CaseRecord) -> None:
    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    text = "\n".join([record.complaint_text, record.log_text, json.dumps(raw, ensure_ascii=False)])
    emails = [e for e in extract_emails(text) if e.lower() != record.consumer_email.lower()]
    phones = [p for p in extract_phones(text) if p != record.consumer_phone]
    if emails or phones:
        with st.expander("Extracted contact clues from Complaint Warrior", expanded=False):
            if emails:
                st.write({"emails": emails})
            if phones:
                st.write({"phones": phones})
            st.caption("These clues are not a substitute for the defendant’s legal name or physical service address.")



def editable_case_form(record: CaseRecord, conn: sqlite3.Connection) -> FilingPacket | None:
    st.subheader("3) Review and generate court packet")
    tab1, tab2, tab3, tab4 = st.tabs(["Intake", "SC-100 draft", "Evidence", "Workflow"])

    saved_packet_row = load_latest_saved_packet(conn, record.case_id)
    saved_sc100 = (saved_packet_row or {}).get("packet_json_parsed") or {}
    if saved_packet_row:
        saved_dt = datetime.fromtimestamp(saved_packet_row["saved_at"]).strftime("%Y-%m-%d %H:%M:%S")
        st.caption(f"Latest saved small-claims packet found for this case: packet #{saved_packet_row['packet_id']} saved {saved_dt}.")

    claim_summary_default = saved_sc100.get("claim_summary") or infer_short_reason(record.complaint_text, record.log_text, record.company_name)
    claim_detail_default = saved_sc100.get("claim_detail") or first_non_empty([record.complaint_text, record.log_text, claim_summary_default])
    plaintiff_address_default = saved_sc100.get("plaintiff_street") and assemble_address(
        saved_sc100.get("plaintiff_street", ""),
        saved_sc100.get("plaintiff_city", ""),
        saved_sc100.get("plaintiff_state", ""),
        saved_sc100.get("plaintiff_zip", ""),
    ) or record.consumer_address
    defendant_address_default = saved_sc100.get("defendant_street") and assemble_address(
        saved_sc100.get("defendant_street", ""),
        saved_sc100.get("defendant_city", ""),
        saved_sc100.get("defendant_state", ""),
        saved_sc100.get("defendant_zip", ""),
    ) or ""

    with tab1:
        raw = record.raw_row if isinstance(record.raw_row, dict) else {}
        subject_hint = clean_text(raw.get("subject"))
        final_hint = clean_text(raw.get("final_conclusion"))
        if subject_hint or final_hint:
            st.info(
                " | ".join(
                    [x for x in [f"Complaint Warrior subject: {subject_hint}" if subject_hint else "", f"Final conclusion: {final_hint}" if final_hint else ""] if x]
                )
            )
        extracted_contacts_ui(record)

        col1, col2 = st.columns(2)
        with col1:
            plaintiff_name = st.text_input("Plaintiff name", value=saved_sc100.get("plaintiff_name") or record.consumer_name)
            plaintiff_address = st.text_area("Plaintiff mailing address", value=plaintiff_address_default, height=90)
            plaintiff_email = st.text_input("Plaintiff email", value=saved_sc100.get("plaintiff_email") or record.consumer_email)
            plaintiff_phone = st.text_input("Plaintiff phone", value=saved_sc100.get("plaintiff_phone") or record.consumer_phone)
        with col2:
            company_name = st.text_input("Company / defendant display name", value=saved_sc100.get("company_name") or record.company_name)
            defendant_legal_name = st.text_input("Defendant legal name", value=saved_sc100.get("defendant_legal_name") or record.company_name)
            defendant_address = st.text_area("Defendant / service address", value=defendant_address_default, height=90)
            agent_for_service = st.text_input("Agent for service of process", value=saved_sc100.get("agent_for_service") or "")

        col3, col4, col5 = st.columns([1.1, 1, 1])
        with col3:
            county = st.selectbox(
                "County where to file",
                options=[""] + COUNTY_OPTIONS,
                index=(COUNTY_OPTIONS.index((saved_sc100.get("county") or record.county)) + 1) if (saved_sc100.get("county") or record.county) in COUNTY_OPTIONS else 0,
            )
        with col4:
            amount = st.number_input("Amount claimed", min_value=0.0, value=float(saved_sc100.get("amount") or record.amount), step=50.0)
        with col5:
            demand_date = st.text_input("Date of final demand", value=saved_sc100.get("demand_date") or record.demand_date)

        county_basis = st.text_area(
            "Why this county is proper venue",
            value=saved_sc100.get("county_basis") or infer_county_basis(county or record.county, record.county_basis, company_name),
            height=90,
        )
        claim_summary = st.text_area("Short claim statement", value=claim_summary_default, height=90)
        claim_detail = st.text_area("Detailed claim narrative", value=claim_detail_default, height=200)

        complaint_text = st.text_area("Complaint text", value=saved_sc100.get("complaint_text") or record.complaint_text, height=180)
        log_text = st.text_area("Complaint Warrior negotiation / outreach log", value=saved_sc100.get("log_text") or record.log_text, height=260)

        updated_record = CaseRecord(
            case_id=record.case_id,
            company_name=company_name,
            amount=amount,
            county=county,
            status=record.status,
            complaint_text=complaint_text,
            log_text=log_text,
            consumer_name=plaintiff_name,
            consumer_email=plaintiff_email,
            consumer_phone=plaintiff_phone,
            consumer_address=plaintiff_address,
            county_basis=county_basis,
            demand_date=demand_date,
            raw_row=record.raw_row,
        )

        if st.button("Generate packet", type="primary"):
            packet = build_packet(
                record=updated_record,
                plaintiff_name=plaintiff_name,
                plaintiff_address=plaintiff_address,
                plaintiff_email=plaintiff_email,
                plaintiff_phone=plaintiff_phone,
                defendant_legal_name=defendant_legal_name,
                defendant_address=defendant_address,
                agent_for_service=agent_for_service,
                demand_date=demand_date,
                county_basis=county_basis,
                amount=amount,
                county=county,
                claim_summary=claim_summary,
                claim_detail=claim_detail,
            )
            st.session_state["generated_packet"] = packet
            st.success("Packet generated below.")

    packet = st.session_state.get("generated_packet")
    if not isinstance(packet, FilingPacket) or packet.sc100.get("case_id") != record.case_id:
        packet = None

    with tab2:
        if packet:
            st.markdown(packet.claim_markdown)
            st.download_button(
                "Download SC-100 draft markdown",
                data=text_bytes(packet.claim_markdown),
                file_name=f"sc100_draft_{packet.sc100['case_id']}.md",
                mime="text/markdown",
            )
            st.download_button(
                "Download SC-100 JSON",
                data=json_bytes(packet.sc100),
                file_name=f"sc100_payload_{packet.sc100['case_id']}.json",
                mime="application/json",
            )
        else:
            st.info("Generate the packet in the Intake tab first.")

    with tab3:
        if packet:
            dmg_df = pd.DataFrame(packet.damages_rows)
            exh_df = pd.DataFrame(packet.exhibit_rows)
            tl_df = pd.DataFrame(packet.timeline_rows)
            st.markdown("**Damages table**")
            st.dataframe(dmg_df, use_container_width=True, hide_index=True)
            st.markdown("**Exhibit list**")
            st.dataframe(exh_df, use_container_width=True, hide_index=True)
            st.markdown("**Timeline**")
            st.dataframe(tl_df, use_container_width=True, hide_index=True)
        else:
            st.info("Generate the packet in the Intake tab first.")

    with tab4:
        if packet:
            st.markdown(packet.workflow_markdown)
        else:
            st.info("Generate the packet in the Intake tab first.")

    return packet



def downloads_ui(packet: FilingPacket, conn: sqlite3.Connection, temp_path: Optional[str]) -> None:
    st.subheader("4) Downloads")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Build packet PDF"):
            try:
                packet_pdf = make_packet_pdf(packet)
                st.session_state["packet_pdf"] = packet_pdf
                st.success("Packet PDF generated.")
            except Exception as exc:
                st.error(f"Could not generate packet PDF: {exc}")
        if isinstance(st.session_state.get("packet_pdf"), (bytes, bytearray)):
            st.download_button(
                "Download packet PDF",
                data=st.session_state["packet_pdf"],
                file_name=f"small_claim_packet_{packet.sc100['case_id']}.pdf",
                mime="application/pdf",
            )

    with col2:
        source_mode = st.radio(
            "SC-100 blank form source",
            options=["Upload blank SC-100 PDF", "Fetch official blank SC-100 PDF"],
            horizontal=False,
        )
        uploaded_pdf = None
        if source_mode == "Upload blank SC-100 PDF":
            uploaded_pdf = st.file_uploader("Upload blank official SC-100 PDF", type=["pdf"])
        if st.button("Populate blank SC-100 PDF"):
            try:
                if source_mode == "Upload blank SC-100 PDF":
                    if uploaded_pdf is None:
                        raise ValueError("Upload the blank SC-100 PDF first.")
                    blank_pdf = uploaded_pdf.getvalue()
                else:
                    blank_pdf = fetch_official_sc100_pdf()
                field_names = extract_pdf_fields(blank_pdf)
                field_map = default_field_map(field_names, packet.sc100)
                filled_pdf = fill_pdf_form(blank_pdf, field_map)
                st.session_state["filled_sc100_pdf"] = filled_pdf
                st.session_state["detected_pdf_fields"] = field_names
                st.success("Best-effort SC-100 PDF generated. Review every field before filing.")
            except Exception as exc:
                st.error(f"Could not populate SC-100 PDF: {exc}")

        if isinstance(st.session_state.get("filled_sc100_pdf"), (bytes, bytearray)):
            st.download_button(
                "Download populated SC-100 PDF",
                data=st.session_state["filled_sc100_pdf"],
                file_name=f"sc100_filled_{packet.sc100['case_id']}.pdf",
                mime="application/pdf",
            )

    with st.expander("Detected PDF fields from blank SC-100", expanded=False):
        field_names = st.session_state.get("detected_pdf_fields")
        if field_names:
            st.write(field_names)
        else:
            st.caption("Populate a blank PDF first to inspect detected field names.")

    zip_bytes = build_download_zip(
        packet=packet,
        filled_pdf=st.session_state.get("filled_sc100_pdf"),
        packet_pdf=st.session_state.get("packet_pdf"),
    )
    st.download_button(
        "Download complete court packet ZIP",
        data=zip_bytes,
        file_name=f"small_claim_court_packet_{packet.sc100['case_id']}.zip",
        mime="application/zip",
        type="primary",
    )

    st.markdown("---")
    st.markdown("**Save back into Complaint Warrior database**")
    st.caption("This writes the generated packet into a new table named `small_claim_packets`. If you connected by upload, download the updated SQLite file after saving.")
    save_col1, save_col2 = st.columns([1, 1])
    with save_col1:
        if st.button("Save packet into Complaint Warrior DB"):
            try:
                save_info = save_packet_to_db(
                    conn=conn,
                    packet=packet,
                    packet_pdf=st.session_state.get("packet_pdf"),
                    filled_sc100_pdf=st.session_state.get("filled_sc100_pdf"),
                )
                st.session_state["saved_packet_info"] = save_info
                st.success(f"Saved packet #{save_info['packet_id']} for case {save_info['complaint_id']} into small_claim_packets.")
            except Exception as exc:
                st.error(f"Could not save packet into Complaint Warrior DB: {exc}")
    saved_info = st.session_state.get("saved_packet_info")
    if isinstance(saved_info, dict) and saved_info.get("complaint_id") == packet.sc100.get("case_id"):
        saved_dt = datetime.fromtimestamp(saved_info["saved_at"]).strftime("%Y-%m-%d %H:%M:%S")
        with save_col2:
            st.info(f"Latest save in this session: packet #{saved_info['packet_id']} at {saved_dt}.")
        if temp_path:
            try:
                updated_db_bytes = Path(temp_path).read_bytes()
                st.download_button(
                    "Download updated Complaint Warrior DB",
                    data=updated_db_bytes,
                    file_name="cw_store_with_small_claim_packets.sqlite",
                    mime="application/x-sqlite3",
                )
            except Exception as exc:
                st.warning(f"Packet was saved, but the updated DB file could not be prepared for download: {exc}")



def main() -> None:
    init_page()
    st.info(
        "This app drafts court paperwork and evidence summaries. A human still needs to verify facts, defendant identity, venue, filing method, and service."
    )
    conn, temp_path = connection_ui()
    if conn is None:
        st.stop()

    try:
        if is_complaint_warrior_db(conn):
            _records, record = native_case_browser_ui(conn)
        else:
            _, mapping, preview_df = select_mapping_ui(conn)
            if preview_df is None:
                st.stop()
            records = build_case_records(preview_df, mapping)
            record = select_case_ui(records)
        if record is None:
            st.stop()

        packet = editable_case_form(record, conn)
        if packet:
            downloads_ui(packet, conn, temp_path)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
