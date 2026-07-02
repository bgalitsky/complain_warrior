from __future__ import annotations

import io
import json
import hashlib
import os
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


EFILE_PROVIDER_DIRECTORY_URL = "https://www.odysseyefileca.com/service-providers.htm"
EFILE_ACTIVE_COURTS_URL = "https://www.odysseyefileca.com/active-courts.htm"
EFILE_ROUTE_VERIFIED_DATE = "2026-07-01"

# Conservative allow-list for the application's assisted e-filing workflow.
# A county is included only when its public court materials identify Small Claims
# as an electronically fileable case type. The app still requires a live check
# at submission time because courts can exclude individual filing codes.
CONFIRMED_CA_SMALL_CLAIMS_EFILE_COUNTIES = {
    "Alameda", "Butte", "Calaveras", "Contra Costa", "El Dorado", "Fresno",
    "Imperial", "Inyo", "Kern", "Kings", "Lake", "Los Angeles", "Madera",
    "Marin", "Mendocino", "Mono", "Monterey", "Nevada", "Orange", "Placer",
    "Riverside", "Sacramento", "San Benito", "San Bernardino", "San Joaquin",
    "San Luis Obispo", "Santa Clara", "Santa Cruz", "Siskiyou", "Stanislaus",
    "Tulare", "Ventura", "Yolo", "Yuba",
}

ODYSSEY_EFILECA_COUNTIES = {
    "Alameda", "Butte", "Calaveras", "Contra Costa", "Fresno", "Kern", "Kings",
    "Los Angeles", "Mendocino", "Monterey", "Orange", "San Bernardino",
    "San Luis Obispo", "Santa Clara", "Santa Cruz", "Stanislaus", "Yolo", "Yuba",
}

COUNTY_EFILE_INFO_URLS = {
    "Calaveras": "https://calaveras.courts.ca.gov/online-services/efiling-information",
    "El Dorado": "https://www.eldorado.courts.ca.gov/online-services/efiling",
    "Lake": "https://lake.courts.ca.gov/efiling",
    "Madera": "https://www.madera.courts.ca.gov/online-services/efiling-dvgv-petition-online-case-information",
    "Marin": "https://www.marin.courts.ca.gov/online-services/efiling",
    "Placer": "https://www.placer.courts.ca.gov/online-services/efiling",
    "Riverside": "https://www.riverside.courts.ca.gov/how-file-small-claims",
    "San Bernardino": "https://sanbernardino.courts.ca.gov/efiling/small-claims-efiling",
    "Ventura": "https://ventura.courts.ca.gov/online-services/efiling",
}

EFILE_STATUS_OPTIONS = [
    "READY_FOR_EFSP",
    "SUBMITTED_TO_EFSP",
    "RECEIVED_BY_COURT",
    "ACCEPTED",
    "REJECTED",
    "WITHDRAWN",
]

FILING_CODE_SUGGESTIONS = {
    "SC-100": "Plaintiff's Claim and Order to Go to Small Claims Court",
    "SC-100A": "Other Plaintiffs or Defendants",
    "SC-103": "Fictitious Business Name declaration",
    "FW-001": "Request to Waive Court Fees",
    "FW-003": "Order on Court Fee Waiver",
    "SC-104": "Proof of Service",
    "OTHER": "Other small-claims filing; select the exact EFSP filing code",
}

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
# Complaint Warrior native status update helper
# -----------------------------


def append_native_complaint_activity_conn(
    conn: sqlite3.Connection,
    complaint_id: str,
    *,
    title: str,
    detail: str,
    kind: str = "status",
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append a Small Claims activity without falsely changing module status."""
    if not complaint_id:
        return False
    cols = [r[1] for r in conn.execute("PRAGMA table_info('complaints')").fetchall()]
    if "complaint_id" not in cols or "complaint_json" not in cols:
        return False
    row = conn.execute(
        "SELECT complaint_json FROM complaints WHERE complaint_id=? LIMIT 1",
        (complaint_id,),
    ).fetchone()
    if not row:
        return False
    obj = json_loads_safe(row[0])
    activities = obj.get("activities")
    if not isinstance(activities, list):
        activities = []
    activities.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "channel": "small_claims",
        "kind": kind,
        "title": clean_text(title),
        "detail": clean_text(detail),
        "meta": meta or {},
    })
    obj["activities"] = activities
    conn.execute(
        "UPDATE complaints SET complaint_json=?, updated_at=? WHERE complaint_id=?",
        (json.dumps(obj, ensure_ascii=False), time.time(), complaint_id),
    )
    conn.commit()
    return True

MODULE_STATUS_LABELS = {
    "resolved": "Resolved",
    "social_network_shared": "Social network shared",
    "charge_back_initiated": "Charge-back initiated",
    "submitted_to_small_claim_court": "Submitted to small claim court",
    "escalated_to_authorities": "Escalated to authorities",
}

def update_native_complaint_module_status_conn(
    conn: sqlite3.Connection,
    complaint_id: str,
    status_key: str,
    note: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Update Complaint Warrior native complaints.complaint_json in the open DB."""
    if status_key not in MODULE_STATUS_LABELS or not complaint_id:
        return False
    label = MODULE_STATUS_LABELS[status_key]
    now_iso = datetime.now().isoformat(timespec="seconds")
    cols = [r[1] for r in conn.execute("PRAGMA table_info('complaints')").fetchall()]
    if "complaint_id" not in cols or "complaint_json" not in cols:
        return False
    row = conn.execute(
        "SELECT complaint_json FROM complaints WHERE complaint_id=? LIMIT 1",
        (complaint_id,),
    ).fetchone()
    if not row:
        return False
    obj = json_loads_safe(row[0])
    statuses = obj.get("module_statuses")
    if not isinstance(statuses, dict):
        statuses = {}
    for key, key_label in MODULE_STATUS_LABELS.items():
        statuses.setdefault(key, {"done": False, "label": key_label, "updated_at": None, "note": ""})
        statuses[key]["label"] = key_label
    statuses[status_key].update({
        "done": True,
        "label": label,
        "updated_at": now_iso,
        "note": note or "",
    })
    obj["module_statuses"] = statuses
    resolved_done = bool((statuses.get("resolved") or {}).get("done"))
    already_resolved = resolved_done or "resolved" in str(obj.get("final_conclusion", "")).lower()
    if not already_resolved:
        obj["current_status_summary"] = label
        if status_key == "submitted_to_small_claim_court":
            obj["final_conclusion"] = "Submitted to small claim court; awaiting hearing or settlement."
            for thread in (obj.get("threads") or {}).values():
                if isinstance(thread, dict):
                    thread["status"] = "submitted"
                    thread["stage"] = "submitted_to_small_claim_court"
                    thread["drafts"] = []
                    thread["last_decision"] = None
    activities = obj.get("activities")
    if not isinstance(activities, list):
        activities = []
    activities.append({
        "ts": now_iso,
        "channel": "small_claims",
        "kind": "status",
        "title": label,
        "detail": note or "Small-claims packet saved/submitted.",
        "meta": {"status_key": status_key, **(meta or {})},
    })
    obj["activities"] = activities
    conn.execute(
        "UPDATE complaints SET complaint_json=?, updated_at=? WHERE complaint_id=?",
        (json.dumps(obj, ensure_ascii=False), time.time(), complaint_id),
    )
    conn.commit()
    return True

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
                "title": record_title(r),
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
            # California courts commonly reject PDFs that retain active fillable fields.
            # pypdf >= 5 supports flatten=True; keep a fallback for earlier versions.
            try:
                writer.update_page_form_field_values(
                    page, field_value_map, auto_regenerate=False, flatten=True
                )
            except TypeError:
                writer.update_page_form_field_values(page, field_value_map)
        except Exception:
            continue

    # pypdf renders field appearances with flatten=True but can retain the
    # AcroForm/widget dictionaries. Remove those dictionaries after rendering
    # so the resulting PDF is no longer an interactive form.
    for page in writer.pages:
        try:
            annots = page.get("/Annots")
            if not annots:
                continue
            for idx in range(len(annots) - 1, -1, -1):
                try:
                    annot = annots[idx].get_object()
                    if str(annot.get("/Subtype")) == "/Widget":
                        del annots[idx]
                except Exception:
                    continue
            if not annots and "/Annots" in page:
                del page["/Annots"]
        except Exception:
            continue
    try:
        if "/AcroForm" in writer._root_object:
            del writer._root_object["/AcroForm"]
    except Exception:
        pass

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
# California Small Claims e-filing handoff
# -----------------------------

def get_small_claim_efile_route(county: str) -> dict[str, Any]:
    county = clean_text(county)
    supported = county in CONFIRMED_CA_SMALL_CLAIMS_EFILE_COUNTIES
    platform = "Odyssey eFileCA" if county in ODYSSEY_EFILECA_COUNTIES else "Court-approved EFSP"
    return {
        "county": county,
        "supported": supported,
        "platform": platform if supported else "Manual / verify with court",
        "provider_directory_url": EFILE_PROVIDER_DIRECTORY_URL,
        "court_info_url": COUNTY_EFILE_INFO_URLS.get(county, ""),
        "verified_on": EFILE_ROUTE_VERIFIED_DATE,
        "initial_sc100_supported": supported,
        "live_verification_required": True,
    }


def infer_filing_code(filename: str) -> str:
    upper = clean_text(filename).upper().replace("_", "-")
    for code in ["SC-100A", "SC-100", "SC-103", "FW-001", "FW-003", "SC-104"]:
        if code in upper or code.replace("-", "") in upper.replace("-", ""):
            return code
    return "OTHER"


def safe_efile_filename(filename: str, fallback: str = "document.pdf") -> str:
    filename = Path(clean_text(filename) or fallback).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "document.pdf"
    if not stem.lower().endswith(".pdf"):
        stem += ".pdf"
    return stem[:120]


def validate_efile_pdf(filename: str, data: bytes, max_document_mb: int = 25) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    page_count = 0
    text_chars = 0
    field_count = 0
    encrypted = False
    metadata_present = False

    if not data or not data.startswith(b"%PDF"):
        errors.append("The file is not a recognizable PDF.")
    if len(data) > max_document_mb * 1024 * 1024:
        errors.append(f"The PDF exceeds the {max_document_mb} MB per-document limit.")

    if not errors and PdfReader is not None:
        try:
            reader = PdfReader(io.BytesIO(data))
            encrypted = bool(reader.is_encrypted)
            if encrypted:
                errors.append("Password-protected/encrypted PDFs are not accepted for e-filing.")
            else:
                page_count = len(reader.pages)
                if page_count == 0:
                    errors.append("The PDF contains no pages.")
                for page in reader.pages:
                    try:
                        text_chars += len(clean_text(page.extract_text()))
                    except Exception:
                        pass
                try:
                    field_count = len(reader.get_fields() or {})
                except Exception:
                    field_count = 0
                metadata_present = bool(reader.metadata)
        except Exception as exc:
            errors.append(f"The PDF could not be parsed: {exc}")
    elif PdfReader is None:
        warnings.append("pypdf is unavailable, so structural PDF checks were skipped.")

    if field_count:
        warnings.append(
            f"The PDF still contains {field_count} active form field(s). Flatten it before court submission."
        )
    if page_count and text_chars < max(20, page_count * 8):
        warnings.append("Very little searchable text was detected; verify that the PDF is text-searchable/OCR'd.")
    if metadata_present:
        warnings.append("PDF metadata is present. Consider sanitizing author/editing metadata before filing.")

    return {
        "filename": safe_efile_filename(filename),
        "size_bytes": len(data),
        "size_mb": round(len(data) / (1024 * 1024), 3),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pages": page_count,
        "text_chars": text_chars,
        "active_form_fields": field_count,
        "encrypted": encrypted,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors and field_count == 0,
    }


def build_efiling_manifest(
    packet: FilingPacket,
    provider_name: str,
    filing_stage: str,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    route = get_small_claim_efile_route(packet.sc100.get("county", ""))
    return {
        "schema": "complaint-warrior.ca-small-claims-efile.v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "route_verified_on": EFILE_ROUTE_VERIFIED_DATE,
        "complaint_id": packet.sc100.get("case_id"),
        "county": packet.sc100.get("county"),
        "court_case_type": "Small Claims",
        "filing_stage": filing_stage,
        "provider_name": provider_name,
        "platform": route["platform"],
        "plaintiff": {
            "name": packet.sc100.get("plaintiff_name"),
            "email": packet.sc100.get("plaintiff_email"),
            "phone": packet.sc100.get("plaintiff_phone"),
        },
        "defendant": {
            "legal_name": packet.sc100.get("defendant_legal_name"),
            "agent_for_service": packet.sc100.get("agent_for_service"),
        },
        "amount": packet.sc100.get("amount"),
        "documents": [
            {
                "filename": d["filename"],
                "filing_code_suggestion": d["filing_code"],
                "filing_description": FILING_CODE_SUGGESTIONS.get(d["filing_code"], FILING_CODE_SUGGESTIONS["OTHER"]),
                "sha256": d["validation"]["sha256"],
                "size_bytes": d["validation"]["size_bytes"],
                "pages": d["validation"]["pages"],
            }
            for d in documents
        ],
        "attestations_required": [
            "The filer reviewed all facts and signatures.",
            "The filer selected the exact EFSP filing code and security level.",
            "Confidential identifiers were removed or redacted.",
            "The selected county and courthouse are proper venue.",
        ],
    }


def build_efiling_handoff_zip(manifest: dict[str, Any], documents: list[dict[str, Any]]) -> bytes:
    readme = textwrap.dedent(
        f"""
        California Small Claims e-filing handoff

        Complaint ID: {manifest.get('complaint_id', '')}
        County: {manifest.get('county', '')}
        Filing stage: {manifest.get('filing_stage', '')}
        Suggested provider/platform: {manifest.get('provider_name', '')} / {manifest.get('platform', '')}

        Steps:
        1. Open the court-approved EFSP/provider directory.
        2. Create or sign into an EFSP account.
        3. Start a Small Claims filing in the county shown above.
        4. Upload each PDF separately when it needs its own file stamp.
        5. Select the exact filing code in the EFSP; manifest codes are suggestions only.
        6. Review fees, service options, privacy/security level, and submit.
        7. Return to Complaint Warrior and record the EFSP envelope/transaction number.

        Do not email these PDFs to a clerk unless that court expressly instructs you to do so.
        """
    ).strip()
    out = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("efile_manifest.json", json_bytes(manifest))
        zf.writestr("README_EFILE.txt", text_bytes(readme))
        for idx, doc in enumerate(documents, start=1):
            base = safe_efile_filename(doc["filename"], f"document_{idx}.pdf")
            name = base
            suffix = 2
            while name.lower() in used:
                name = f"{Path(base).stem}_{suffix}.pdf"
                suffix += 1
            used.add(name.lower())
            zf.writestr(f"documents/{idx:02d}_{name}", doc["data"])
    return out.getvalue()


def ensure_small_claim_efilings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS small_claim_efilings (
            filing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            complaint_id TEXT NOT NULL,
            packet_id INTEGER,
            county TEXT NOT NULL,
            filing_stage TEXT NOT NULL,
            provider_name TEXT,
            provider_url TEXT,
            platform TEXT,
            status TEXT NOT NULL,
            envelope_reference TEXT,
            court_case_number TEXT,
            rejection_reason TEXT,
            manifest_json TEXT NOT NULL,
            handoff_zip BLOB,
            receipt_pdf BLOB,
            provider_response_json TEXT,
            submitted_at REAL,
            accepted_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_small_claim_efilings_case ON small_claim_efilings(complaint_id, updated_at DESC)"
    )
    conn.commit()


def create_efiling_record(
    conn: sqlite3.Connection,
    *,
    manifest: dict[str, Any],
    handoff_zip: bytes,
    provider_url: str,
    status: str = "READY_FOR_EFSP",
    provider_response: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ensure_small_claim_efilings_table(conn)
    now = time.time()
    packet_id = None
    if table_exists(conn, "small_claim_packets"):
        row = conn.execute(
            "SELECT packet_id FROM small_claim_packets WHERE complaint_id=? ORDER BY is_latest DESC, saved_at DESC LIMIT 1",
            (clean_text(manifest.get("complaint_id")),),
        ).fetchone()
        packet_id = row[0] if row else None
    submitted_at = now if status in {"SUBMITTED_TO_EFSP", "RECEIVED_BY_COURT", "ACCEPTED"} else None
    accepted_at = now if status == "ACCEPTED" else None
    cur = conn.execute(
        """
        INSERT INTO small_claim_efilings (
            complaint_id, packet_id, county, filing_stage, provider_name, provider_url,
            platform, status, manifest_json, handoff_zip, provider_response_json,
            submitted_at, accepted_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_text(manifest.get("complaint_id")), packet_id, clean_text(manifest.get("county")),
            clean_text(manifest.get("filing_stage")), clean_text(manifest.get("provider_name")),
            provider_url, clean_text(manifest.get("platform")), status,
            json.dumps(manifest, ensure_ascii=False), handoff_zip,
            json.dumps(provider_response or {}, ensure_ascii=False), submitted_at, accepted_at, now, now,
        ),
    )
    conn.commit()
    return {"filing_id": cur.lastrowid, "status": status, "created_at": now}


def latest_efiling_record(conn: sqlite3.Connection, complaint_id: str) -> Optional[dict[str, Any]]:
    if not table_exists(conn, "small_claim_efilings"):
        return None
    row = conn.execute(
        "SELECT * FROM small_claim_efilings WHERE complaint_id=? ORDER BY updated_at DESC LIMIT 1",
        (complaint_id,),
    ).fetchone()
    return dict(row) if row else None


def update_efiling_record(
    conn: sqlite3.Connection,
    filing_id: int,
    *,
    status: str,
    provider_name: str = "",
    envelope_reference: str = "",
    court_case_number: str = "",
    rejection_reason: str = "",
    receipt_pdf: Optional[bytes] = None,
) -> None:
    if status not in EFILE_STATUS_OPTIONS:
        raise ValueError(f"Unsupported e-filing status: {status}")
    now = time.time()
    submitted_at = now if status in {"SUBMITTED_TO_EFSP", "RECEIVED_BY_COURT", "ACCEPTED"} else None
    accepted_at = now if status == "ACCEPTED" else None
    conn.execute(
        """
        UPDATE small_claim_efilings
        SET status=?, provider_name=COALESCE(NULLIF(?, ''), provider_name),
            envelope_reference=?, court_case_number=?, rejection_reason=?,
            receipt_pdf=COALESCE(?, receipt_pdf),
            submitted_at=COALESCE(submitted_at, ?), accepted_at=COALESCE(accepted_at, ?),
            updated_at=?
        WHERE filing_id=?
        """,
        (
            status, provider_name, envelope_reference, court_case_number, rejection_reason,
            receipt_pdf, submitted_at, accepted_at, now, filing_id,
        ),
    )
    conn.commit()


def configured_efsp_api() -> dict[str, str]:
    return {
        "url": clean_text(os.getenv("CW_EFSP_API_URL")),
        "token": clean_text(os.getenv("CW_EFSP_API_TOKEN")),
        "provider_name": clean_text(os.getenv("CW_EFSP_PROVIDER_NAME")) or "Configured EFSP API",
    }


def submit_to_configured_efsp(
    manifest: dict[str, Any], documents: list[dict[str, Any]]
) -> dict[str, Any]:
    cfg = configured_efsp_api()
    if not cfg["url"]:
        raise RuntimeError("CW_EFSP_API_URL is not configured.")
    if requests is None:
        raise RuntimeError("The requests package is unavailable.")
    headers = {"Accept": "application/json"}
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    files = [
        ("documents", (d["filename"], d["data"], "application/pdf"))
        for d in documents
    ]
    response = requests.post(
        cfg["url"],
        headers=headers,
        data={"metadata": json.dumps(manifest, ensure_ascii=False)},
        files=files,
        timeout=90,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text[:4000]}
    if not isinstance(payload, dict):
        payload = {"response": payload}
    payload["http_status"] = response.status_code
    return payload


def efiling_ui(packet: FilingPacket, conn: sqlite3.Connection, temp_path: Optional[str]) -> None:
    st.subheader("5) California Small Claims e-file submission")
    county = clean_text(packet.sc100.get("county"))
    route = get_small_claim_efile_route(county)

    if not county:
        st.error("Select a filing county in Intake and regenerate the packet before e-filing.")
        return
    if not route["supported"]:
        st.warning(
            f"{county} County is not in this app's confirmed Small Claims e-filing allow-list. "
            "Use mail/in-person filing or verify the exact filing route with the court."
        )
        if route["court_info_url"]:
            st.link_button("Open county filing information", route["court_info_url"])
        return

    st.success(
        f"{county} County is enabled for the assisted e-filing workflow via {route['platform']}. "
        f"Registry last checked {route['verified_on']}."
    )
    st.warning(
        "This is an EFSP handoff, not email-to-court filing. Confirm that the initial SC-100 filing code "
        "is currently available in the selected EFSP before paying and submitting."
    )

    link_col1, link_col2 = st.columns(2)
    with link_col1:
        st.link_button("Open approved EFSP directory", route["provider_directory_url"], use_container_width=True)
    with link_col2:
        if route["court_info_url"]:
            st.link_button("Open county e-filing instructions", route["court_info_url"], use_container_width=True)
        else:
            st.link_button("Open eFileCA active-court information", EFILE_ACTIVE_COURTS_URL, use_container_width=True)

    filing_stage = st.selectbox(
        "Filing stage",
        ["INITIAL_SC100", "SUBSEQUENT_SMALL_CLAIMS_FILING"],
        format_func=lambda x: "Open a new case with SC-100" if x == "INITIAL_SC100" else "File into an existing small-claims case",
        key=f"efile_stage_{packet.sc100.get('case_id')}",
    )
    provider_options = [
        "Choose an approved EFSP from the directory",
        "Odyssey eFileCA / File & Serve provider",
        "Other court-approved EFSP",
    ]
    cfg = configured_efsp_api()
    if cfg["url"]:
        provider_options.append(cfg["provider_name"])
    provider_name = st.selectbox(
        "Provider / submission route",
        provider_options,
        key=f"efile_provider_{packet.sc100.get('case_id')}",
    )

    st.markdown("#### Filing PDFs")
    st.caption(
        "Use the flattened official SC-100 as the lead document. Add each other official form separately "
        "when it needs its own file stamp (for example, FW-001)."
    )
    use_generated = st.checkbox(
        "Include the populated SC-100 generated in Downloads",
        value=True,
        key=f"efile_use_generated_{packet.sc100.get('case_id')}",
    )
    uploaded_lead = st.file_uploader(
        "Upload lead SC-100 PDF instead (optional)",
        type=["pdf"],
        key=f"efile_lead_{packet.sc100.get('case_id')}",
    )
    extra_uploads = st.file_uploader(
        "Additional official PDFs (SC-100A, SC-103, FW-001, local forms)",
        type=["pdf"],
        accept_multiple_files=True,
        key=f"efile_extra_{packet.sc100.get('case_id')}",
    )

    raw_docs: list[tuple[str, bytes]] = []
    if uploaded_lead is not None:
        raw_docs.append((safe_efile_filename(uploaded_lead.name, "SC-100.pdf"), uploaded_lead.getvalue()))
    elif use_generated and isinstance(st.session_state.get("filled_sc100_pdf"), (bytes, bytearray)):
        raw_docs.append((f"SC-100_{packet.sc100.get('case_id')}.pdf", bytes(st.session_state["filled_sc100_pdf"])))
    for upload in extra_uploads or []:
        raw_docs.append((safe_efile_filename(upload.name), upload.getvalue()))

    documents: list[dict[str, Any]] = []
    for name, data in raw_docs:
        validation = validate_efile_pdf(name, data)
        documents.append({
            "filename": validation["filename"],
            "data": data,
            "filing_code": infer_filing_code(name),
            "validation": validation,
        })

    if documents:
        validation_rows = []
        for doc in documents:
            v = doc["validation"]
            validation_rows.append({
                "file": doc["filename"],
                "code suggestion": doc["filing_code"],
                "pages": v["pages"],
                "MB": v["size_mb"],
                "active fields": v["active_form_fields"],
                "result": "OK" if v["ok"] else "Needs attention",
                "errors": "; ".join(v["errors"]),
                "warnings": "; ".join(v["warnings"]),
            })
        st.dataframe(pd.DataFrame(validation_rows), use_container_width=True, hide_index=True)
    else:
        st.info("Generate/populate an SC-100 PDF in Downloads or upload the lead PDF here.")

    total_size = sum(len(d["data"]) for d in documents)
    all_valid = bool(documents) and all(d["validation"]["ok"] for d in documents)
    if total_size > 50 * 1024 * 1024:
        st.error("The combined envelope exceeds 50 MB; split or reduce the PDFs.")
        all_valid = False
    if filing_stage == "INITIAL_SC100" and not any(d["filing_code"] == "SC-100" for d in documents):
        st.error("An initial filing must include an SC-100 lead document.")
        all_valid = False

    manifest = build_efiling_manifest(packet, provider_name, filing_stage, documents) if documents else None
    handoff_zip = build_efiling_handoff_zip(manifest, documents) if manifest else None
    if handoff_zip:
        st.download_button(
            "Download EFSP-ready ZIP",
            data=handoff_zip,
            file_name=f"efile_handoff_{packet.sc100.get('case_id')}_{county}.zip",
            mime="application/zip",
            type="primary",
            disabled=not all_valid,
        )

    attested = st.checkbox(
        "I reviewed the defendant name/address, venue, signatures, redactions, document codes, and filing fee.",
        key=f"efile_attest_{packet.sc100.get('case_id')}",
    )

    action_col1, action_col2 = st.columns(2)
    with action_col1:
        if st.button(
            "Save EFSP handoff record",
            disabled=not (all_valid and attested and manifest and handoff_zip),
            key=f"efile_save_handoff_{packet.sc100.get('case_id')}",
        ):
            info = create_efiling_record(
                conn,
                manifest=manifest,
                handoff_zip=handoff_zip,
                provider_url=route["provider_directory_url"],
                status="READY_FOR_EFSP",
            )
            append_native_complaint_activity_conn(
                conn,
                clean_text(packet.sc100.get("case_id")),
                title="EFSP handoff prepared",
                detail=f"E-filing handoff #{info['filing_id']} prepared for {county} County; not yet submitted.",
                kind="prepared",
                meta={"filing_id": info["filing_id"], "status": info["status"]},
            )
            st.session_state[f"latest_efiling_id_{packet.sc100.get('case_id')}"] = info["filing_id"]
            st.success(f"E-filing handoff #{info['filing_id']} saved. The case is not marked submitted yet.")

    with action_col2:
        api_selected = cfg["url"] and provider_name == cfg["provider_name"]
        if st.button(
            "Submit through configured EFSP API",
            disabled=not (api_selected and all_valid and attested and manifest and handoff_zip),
            key=f"efile_api_submit_{packet.sc100.get('case_id')}",
        ):
            try:
                response_payload = submit_to_configured_efsp(manifest, documents)
                info = create_efiling_record(
                    conn,
                    manifest=manifest,
                    handoff_zip=handoff_zip,
                    provider_url=cfg["url"],
                    status="SUBMITTED_TO_EFSP",
                    provider_response=response_payload,
                )
                envelope_ref = clean_text(
                    response_payload.get("envelope_reference")
                    or response_payload.get("envelope_id")
                    or response_payload.get("transaction_id")
                )
                if envelope_ref:
                    update_efiling_record(
                        conn,
                        info["filing_id"],
                        status="SUBMITTED_TO_EFSP",
                        provider_name=cfg["provider_name"],
                        envelope_reference=envelope_ref,
                    )
                update_native_complaint_module_status_conn(
                    conn,
                    clean_text(packet.sc100.get("case_id")),
                    "submitted_to_small_claim_court",
                    note=f"Submitted through {cfg['provider_name']}; e-filing record #{info['filing_id']}. Envelope: {envelope_ref or 'pending' }.",
                    meta={"filing_id": info["filing_id"], "envelope_reference": envelope_ref},
                )
                st.success(f"Submitted through the configured API. Filing record #{info['filing_id']} created.")
                st.json(response_payload)
            except Exception as exc:
                st.error(f"EFSP API submission failed: {exc}")

    latest = latest_efiling_record(conn, clean_text(packet.sc100.get("case_id")))
    if latest:
        st.markdown("#### Record EFSP receipt or court result")
        st.caption(
            f"Latest filing record #{latest['filing_id']} — {latest['status']} — "
            f"updated {datetime.fromtimestamp(latest['updated_at']).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        c1, c2 = st.columns(2)
        with c1:
            receipt_provider = st.text_input(
                "Actual EFSP/provider name",
                value=clean_text(latest.get("provider_name")),
                key=f"efile_receipt_provider_{latest['filing_id']}",
            )
            envelope_reference = st.text_input(
                "EFSP envelope / transaction number",
                value=clean_text(latest.get("envelope_reference")),
                key=f"efile_envelope_{latest['filing_id']}",
            )
            court_case_number = st.text_input(
                "Court case number (when assigned)",
                value=clean_text(latest.get("court_case_number")),
                key=f"efile_case_number_{latest['filing_id']}",
            )
        with c2:
            current_status = latest.get("status") if latest.get("status") in EFILE_STATUS_OPTIONS else "READY_FOR_EFSP"
            new_status = st.selectbox(
                "Filing status",
                EFILE_STATUS_OPTIONS,
                index=EFILE_STATUS_OPTIONS.index(current_status),
                key=f"efile_status_{latest['filing_id']}",
            )
            rejection_reason = st.text_area(
                "Rejection/return reason",
                value=clean_text(latest.get("rejection_reason")),
                height=90,
                key=f"efile_rejection_{latest['filing_id']}",
            )
            receipt_upload = st.file_uploader(
                "EFSP receipt or court notice PDF",
                type=["pdf"],
                key=f"efile_receipt_file_{latest['filing_id']}",
            )

        if st.button("Update filing status", key=f"efile_update_{latest['filing_id']}"):
            if new_status in {"SUBMITTED_TO_EFSP", "RECEIVED_BY_COURT", "ACCEPTED"} and not envelope_reference:
                st.error("Enter the EFSP envelope/transaction number before marking the filing submitted or accepted.")
            else:
                update_efiling_record(
                    conn,
                    latest["filing_id"],
                    status=new_status,
                    provider_name=receipt_provider,
                    envelope_reference=envelope_reference,
                    court_case_number=court_case_number,
                    rejection_reason=rejection_reason,
                    receipt_pdf=receipt_upload.getvalue() if receipt_upload else None,
                )
                if new_status in {"SUBMITTED_TO_EFSP", "RECEIVED_BY_COURT", "ACCEPTED"}:
                    update_native_complaint_module_status_conn(
                        conn,
                        clean_text(packet.sc100.get("case_id")),
                        "submitted_to_small_claim_court",
                        note=(
                            f"{new_status.replace('_', ' ').title()} through {receipt_provider or 'EFSP'}; "
                            f"envelope {envelope_reference}."
                        ),
                        meta={
                            "filing_id": latest["filing_id"],
                            "envelope_reference": envelope_reference,
                            "court_case_number": court_case_number,
                            "filing_status": new_status,
                        },
                    )
                else:
                    append_native_complaint_activity_conn(
                        conn,
                        clean_text(packet.sc100.get("case_id")),
                        title=f"E-filing status: {new_status}",
                        detail=rejection_reason or f"Filing record #{latest['filing_id']} updated.",
                        meta={"filing_id": latest["filing_id"], "filing_status": new_status},
                    )
                st.success("Filing status updated in Complaint Warrior.")
                st.rerun()

    if temp_path:
        try:
            updated_db_bytes = Path(temp_path).read_bytes()
            st.download_button(
                "Download DB with e-filing records",
                data=updated_db_bytes,
                file_name="cw_store_with_small_claim_efilings.sqlite",
                mime="application/x-sqlite3",
                key=f"efile_download_db_{packet.sc100.get('case_id')}",
            )
        except Exception as exc:
            st.warning(f"Could not prepare the updated database for download: {exc}")



# -----------------------------
# User isolation and safe case selection
# -----------------------------
def get_available_user_emails(conn: sqlite3.Connection) -> list[str]:
    """Return user emails present in the connected Complaint Warrior database.

    Privacy rule: external modules must never show complaints until the user
    selects an owner email. This prevents one user's complaints from appearing
    in another user's module session.
    """
    emails: set[str] = set()
    try:
        if is_complaint_warrior_db(conn):
            rows = conn.execute("SELECT user_email, complaint_json FROM complaints").fetchall()
            for user_email, complaint_json in rows:
                if clean_text(user_email):
                    emails.add(clean_text(user_email).lower())
                obj = json_loads_safe(complaint_json)
                if clean_text(obj.get("user_email")):
                    emails.add(clean_text(obj.get("user_email")).lower())
        else:
            table = auto_detect_table(conn)
            if table:
                cols = get_columns(conn, table)
                mapping = auto_detect_columns(cols)
                email_col = mapping.get("consumer_email")
                if email_col:
                    for (email,) in conn.execute(f"SELECT DISTINCT [{email_col}] FROM [{table}] WHERE [{email_col}] IS NOT NULL"):
                        if clean_text(email):
                            emails.add(clean_text(email).lower())
    except Exception:
        pass
    return sorted(emails)


def mandatory_user_filter_ui(conn: sqlite3.Connection) -> Optional[str]:
    """Require typed user email; do not expose a dropdown of all users."""
    st.subheader("0) Enter Complaint Warrior user email")
    if not get_available_user_emails(conn):
        st.error("No user emails were found in the connected database. Refusing to show complaints without a user filter.")
        return None
    typed = st.text_input(
        "Your Complaint Warrior email",
        value=st.session_state.get("mandatory_cw_user_email", ""),
        placeholder="you@example.com",
        key="mandatory_cw_user_email",
        help="Only cases owned by this exact email will be shown. Other users are never listed.",
    ).strip().lower()
    if not typed:
        st.warning("Enter your Complaint Warrior email before any complaints are displayed.")
        return None
    return typed


def record_owner_email(record: CaseRecord) -> str:
    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    return clean_text(raw.get("user_email") or record.consumer_email).lower()


def filter_records_by_user(records: list[CaseRecord], user_email: str) -> list[CaseRecord]:
    user_email = clean_text(user_email).lower()
    return [r for r in records if record_owner_email(r) == user_email]


def record_title(record: CaseRecord) -> str:
    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    return first_non_empty([
        raw.get("subject"),
        raw.get("title"),
        record.company_name,
        record.complaint_text[:90],
        "Untitled complaint",
    ])


def readable_case_label(record: CaseRecord) -> str:
    return f"{record.case_id} — {record_title(record)}"


def selectable_cases_df(records: list[CaseRecord]) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append({
            "complaint_id": r.case_id,
            "title": record_title(r),
            "company_name": r.company_name,
            "amount": r.amount,
            "county": r.county,
            "status": r.status,
            "consumer_email": r.consumer_email,
            "demand_date": r.demand_date,
        })
    return pd.DataFrame(rows)


def select_case_from_grid_and_dropdown(records: list[CaseRecord], label: str) -> Optional[CaseRecord]:
    if not records:
        st.warning("No complaints are available for the selected user/filter.")
        return None

    df = selectable_cases_df(records)
    st.caption("Select a row in the grid, or use the dropdown below. The dropdown shows complaint id and title.")
    selected_from_grid = None
    try:
        event = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            key=f"{label}_grid",
            on_select="rerun",
            selection_mode="single-row",
        )
        rows = getattr(event, "selection", {}).get("rows", []) if event is not None else []
        if rows:
            selected_from_grid = df.iloc[int(rows[0])]["complaint_id"]
    except TypeError:
        st.dataframe(df, use_container_width=True, hide_index=True)

    ids = [r.case_id for r in records]
    if selected_from_grid in ids:
        st.session_state[f"{label}_selected_case_id"] = selected_from_grid

    labels = [readable_case_label(r) for r in records]
    current_id = st.session_state.get(f"{label}_selected_case_id")
    current_index = ids.index(current_id) if current_id in ids else 0
    selected_label = st.selectbox(
        "Choose case",
        labels,
        index=current_index,
        key=f"{label}_dropdown",
    )
    selected_record = records[labels.index(selected_label)]
    st.session_state[f"{label}_selected_case_id"] = selected_record.case_id
    return selected_record


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



def native_case_browser_ui(conn: sqlite3.Connection, user_email: str) -> tuple[list[CaseRecord], Optional[CaseRecord]]:
    records_all = build_native_case_records(conn)
    records = filter_records_by_user(records_all, user_email)
    st.subheader("1) Complaint Warrior cases")
    st.caption("Only complaints owned by the selected Complaint Warrior user are shown.")

    if not records:
        st.warning(f"No Complaint Warrior cases found for {user_email}.")
        return records_all, None

    only_ready = st.checkbox("Show only cases likely ready for small claims", value=True)
    visible = [r for r in records if record_is_exhausted(r)] if only_ready else records
    if not visible:
        st.info("No cases for this user were clearly marked as exhausted. Showing this user's cases instead.")
        visible = records

    selected = select_case_from_grid_and_dropdown(visible, "native_small_claims")
    return records_all, selected


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



def select_case_ui(records: list[CaseRecord], user_email: str) -> Optional[CaseRecord]:
    records = filter_records_by_user(records, user_email)
    if not records:
        st.warning(f"No rows were loaded for selected user {user_email}.")
        return None

    only_exhausted = st.checkbox("Show only exhausted / unresolved cases", value=True)
    visible = [r for r in records if record_is_exhausted(r)] if only_exhausted else records
    if not visible:
        st.info("No rows matched the exhausted-case filter for this user. Showing this user's rows instead.")
        visible = records

    return select_case_from_grid_and_dropdown(visible, "generic_small_claims")


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



# -----------------------------
# Process serving helpers
# -----------------------------
US_STATE_OPTIONS = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}

STATE_BUSINESS_SEARCH_URLS = {
    "California": "https://bizfileonline.sos.ca.gov/search/business",
    "Delaware": "https://icis.corp.delaware.gov/Ecorp/EntitySearch/NameSearch.aspx",
    "Florida": "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName",
    "Nevada": "https://esos.nv.gov/EntitySearch/OnlineEntitySearch",
    "New York": "https://apps.dos.ny.gov/publicInquiry/",
    "Texas": "https://mycpa.cpa.state.tx.us/coa/",
    "Washington": "https://ccfs.sos.wa.gov/#/BusinessSearch",
    "Oregon": "https://sos.oregon.gov/business/Pages/find.aspx",
    "Arizona": "https://ecorp.azcc.gov/EntitySearch/Index",
    "Colorado": "https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do",
}

STATE_SERVICE_INSTRUCTIONS = {
    "California": [
        "After filing, serve each defendant with a filed copy of SC-100 and any local court attachments.",
        "You personally may not serve the defendant. Use the sheriff, a registered process server, or an adult who is not a party to the case.",
        "For a corporation/LLC, serve the registered agent for service of process or another person authorized by California law.",
        "After service, file Proof of Service (SC-104) with the court before the deadline shown by the court.",
        "Bring a copy of the filed proof of service to the hearing.",
    ],
    "Florida": [
        "After filing, use the clerk/sheriff or a certified process server where required by local practice.",
        "Serve the registered agent or another legally authorized representative of the business.",
        "Keep the return/proof of service and file it with the court as required before the hearing.",
    ],
    "New York": [
        "Follow the small-claims clerk's service procedure; many New York small-claims courts arrange service by mail, but rules vary by court.",
        "For businesses, use the correct legal entity name and a valid service address/registered agent address.",
        "Keep any affidavit/certificate/proof of service and confirm it is filed before the hearing.",
    ],
    "Texas": [
        "Ask the clerk to issue citation after filing, then arrange service by sheriff/constable or authorized private process server.",
        "Serve the registered agent, owner, officer, or other legally authorized representative for the entity type.",
        "Confirm the return of service is filed with the court before requesting default judgment or proceeding to hearing.",
    ],
}


def state_abbreviation(state: str) -> str:
    return STATE_ABBR.get(clean_text(state), clean_text(state).upper()[:2])


def official_business_search_url(state: str) -> str:
    state = clean_text(state)
    return STATE_BUSINESS_SEARCH_URLS.get(state, f"https://www.google.com/search?q={state.replace(' ', '+')}+secretary+of+state+business+search")


def process_server_search_url(state: str, service_address: str = "") -> str:
    query = f"registered process server {clean_text(state)} {clean_text(service_address)}".strip()
    return "https://www.google.com/search?q=" + re.sub(r"\s+", "+", query)


def extract_service_address_candidates(record: CaseRecord, business_name: str = "") -> list[dict[str, str]]:
    """Disabled: do not infer service addresses from free-text complaint logs.

    Prior versions tried to extract street-like strings from complaint/log text.
    That produced false positives from phone numbers, dollar amounts, and free text.
    For process serving, the address must be verified from an official business
    registry, registered-agent record, court-approved source, or entered manually
    by the user.
    """
    return []


def render_service_instructions_markdown(state: str, business_name: str, service_address: str, process_server_name: str = "", process_server_contact: str = "") -> str:
    state = clean_text(state) or "the selected state"
    business_name = clean_text(business_name) or "the defendant business"
    service_address = clean_text(service_address) or "[confirmed service address]"
    process_server_name = clean_text(process_server_name) or "[process server / sheriff / authorized adult]"
    process_server_contact = clean_text(process_server_contact)
    steps = STATE_SERVICE_INSTRUCTIONS.get(state, [
        "Confirm the correct service method with the small-claims clerk or state court self-help site before serving.",
        "Use the defendant's exact legal name and a verified physical service address or registered-agent address.",
        "Do not serve the papers yourself if the state or court requires a non-party, sheriff, constable, or professional process server.",
        "After service, obtain the signed proof/return/affidavit of service and file it with the court before the hearing or default deadline.",
    ])
    lines = [
        f"# Process Serving Instructions — {state}",
        "",
        f"**Defendant/business:** {business_name}",
        f"**Address selected for service:** {service_address}",
        f"**Server:** {process_server_name}",
    ]
    if process_server_contact:
        lines.append(f"**Server contact:** {process_server_contact}")
    lines.extend([
        "",
        "## Steps",
    ])
    lines.extend([f"{idx}. {step}" for idx, step in enumerate(steps, start=1)])
    lines.extend([
        "",
        "## Before handing papers to the server",
        "- Confirm the defendant's exact legal entity name in the official state business registry.",
        "- Confirm whether the address is a registered-agent address, principal office, branch, or other service-eligible address.",
        "- Provide the filed complaint, summons/notice, court date information, and any local forms required by the court.",
        "- Ask the server to return a completed proof/affidavit/return of service with date, time, person served, and method of service.",
        "",
        "This is a workflow aid, not legal advice. Verify the current rule with the court clerk or official state court self-help site.",
    ])
    return "\n".join(lines)


def process_serving_pane(record: CaseRecord, packet: Optional[FilingPacket]) -> None:
    st.markdown("### Process serving")
    st.caption("Find and verify a service address/registered-agent address, then prepare service instructions for the selected state.")

    raw = record.raw_row if isinstance(record.raw_row, dict) else {}
    saved_key = f"process_serving_{record.case_id}"
    saved = st.session_state.get(saved_key, {}) if isinstance(st.session_state.get(saved_key), dict) else {}

    default_state = saved.get("state") or (packet.sc100.get("defendant_state") if packet else "") or raw.get("state") or "California"
    if default_state not in US_STATE_OPTIONS:
        default_state = "California"
    default_business = saved.get("business_name") or (packet.sc100.get("defendant_legal_name") if packet else "") or record.company_name
    default_address = saved.get("service_address") or (packet.sc100.get("defendant_street") if packet else "")
    if packet and packet.sc100.get("defendant_city"):
        default_address = assemble_address(
            packet.sc100.get("defendant_street", ""),
            packet.sc100.get("defendant_city", ""),
            packet.sc100.get("defendant_state", ""),
            packet.sc100.get("defendant_zip", ""),
        )

    c1, c2 = st.columns([1, 1])
    with c1:
        state = st.selectbox("State for defendant/service", US_STATE_OPTIONS, index=US_STATE_OPTIONS.index(default_state), key=f"{saved_key}_state")
        business_name = st.text_input("Business / defendant legal name", value=default_business, key=f"{saved_key}_business")
    with c2:
        st.markdown("**Official lookup links**")
        st.markdown(f"[Open official/state business search]({official_business_search_url(state)})")
        st.markdown(f"[Search for process servers near address]({process_server_search_url(state, default_address)})")
        st.caption("Use the official registry to verify registered agent and service address. Use the process-server search only to choose a server; it does not verify defendant address.")

    st.info(
        "Service address extraction from complaint text is disabled because it can produce false addresses. "
        "Use the official state business search link above to find the registered agent/service address, "
        "then paste the verified address below."
    )

    service_address = st.text_area(
        "Verified service address / registered agent address",
        value=saved.get("service_address") or default_address,
        height=90,
        key=f"{saved_key}_address",
        help="Enter the address verified in the Secretary of State/business registry or court-approved source.",
    )
    process_server_name = st.text_input("Process server / sheriff / constable / authorized adult", value=saved.get("process_server_name", ""), key=f"{saved_key}_server")
    process_server_contact = st.text_input("Process server contact/address (optional)", value=saved.get("process_server_contact", ""), key=f"{saved_key}_server_contact")

    instructions_md = render_service_instructions_markdown(
        state=state,
        business_name=business_name,
        service_address=service_address,
        process_server_name=process_server_name,
        process_server_contact=process_server_contact,
    )

    if st.button("Save process-serving plan", key=f"{saved_key}_save"):
        st.session_state[saved_key] = {
            "state": state,
            "business_name": business_name,
            "service_address": service_address,
            "process_server_name": process_server_name,
            "process_server_contact": process_server_contact,
            "instructions_md": instructions_md,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        st.success("Process-serving plan saved in this session.")

    st.markdown("#### Serving instructions")
    st.markdown(instructions_md)
    st.download_button(
        "Download process-serving instructions",
        data=text_bytes(instructions_md),
        file_name=f"process_serving_{record.case_id}.md",
        mime="text/markdown",
        key=f"{saved_key}_download",
    )


def editable_case_form(record: CaseRecord, conn: sqlite3.Connection) -> FilingPacket | None:
    st.subheader("3) Review and generate court packet")
    tab1, tab2, tab_process, tab3, tab4 = st.tabs(["Intake", "SC-100 draft", "Process serving", "Evidence", "Workflow"])

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

    with tab_process:
        process_serving_pane(record, packet)

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
                activity_saved = append_native_complaint_activity_conn(
                    conn,
                    save_info["complaint_id"],
                    title="Small-claims packet prepared",
                    detail=f"Small-claims packet #{save_info['packet_id']} saved; it has not yet been submitted to the court.",
                    kind="prepared",
                    meta={"packet_id": save_info["packet_id"], "submitted": False},
                )
                st.session_state["saved_packet_info"] = save_info
                if activity_saved:
                    st.success(f"Saved packet #{save_info['packet_id']} for case {save_info['complaint_id']}. Status remains unsubmitted until an EFSP receipt is recorded.")
                else:
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

    selected_user_email = mandatory_user_filter_ui(conn)
    if not selected_user_email:
        st.stop()

    try:
        if is_complaint_warrior_db(conn):
            _records, record = native_case_browser_ui(conn, selected_user_email)
        else:
            _, mapping, preview_df = select_mapping_ui(conn)
            if preview_df is None:
                st.stop()
            records = build_case_records(preview_df, mapping)
            record = select_case_ui(records, selected_user_email)
        if record is None:
            st.stop()

        packet = editable_case_form(record, conn)
        if packet:
            downloads_ui(packet, conn, temp_path)
            st.markdown("---")
            efiling_ui(packet, conn, temp_path)
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
