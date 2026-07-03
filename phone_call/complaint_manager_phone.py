
# complaint_manager_phone_refactored.py
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import base64
import sqlite3
import inspect
import ipaddress
import socket
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from storage import ComplaintStore, CallResultStore
from gmail_token_store import GmailTokenStore
from text_processor import TextProcessing, AgentDecision, SatisfactionDecision, ResolutionStrategy

try:
    from call import ComplaintCallAgent
except Exception:
    ComplaintCallAgent = None

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

LABEL_PROCESSED = "CW_PROCESSED"
TEST_INBOX_EMAIL = "bgalitsky@hotmail.com"

# Deployment safety:
# - In production directory /home/ec2-user/prod, real company/business emails are used.
# - In any other directory, outbound complaint emails are redirected to TEST_INBOX_EMAIL.
# You can override with CW_DEPLOY_ENV=prod or CW_FORCE_TEST_INBOX=1.
PROD_DIR_NAME = os.environ.get("CW_PROD_DIR_NAME", "prod")
FORCE_TEST_INBOX = os.environ.get("CW_FORCE_TEST_INBOX", "").strip().lower() in {"1", "true", "yes", "on"}

AUTO_SEND_POLICIES = ("manual", "draft_only", "auto_send")
DEFAULT_AUTO_SEND_POLICY = "manual"

# Case-aware ChatGPT assistant. The default model is intentionally cost-efficient;
# deployments can choose another Responses API model through CW_ASSISTANT_MODEL.
CASE_ASSISTANT_API_URL = os.environ.get(
    "CW_OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses"
).strip()
CASE_ASSISTANT_MODEL = os.environ.get("CW_ASSISTANT_MODEL", "gpt-5-mini").strip()
CASE_ASSISTANT_TIMEOUT = float(os.environ.get("CW_ASSISTANT_TIMEOUT", "90"))
CASE_ASSISTANT_MAX_CONTEXT_CHARS = int(
    os.environ.get("CW_ASSISTANT_MAX_CONTEXT_CHARS", "70000")
)
# max_output_tokens includes both visible output and reasoning tokens. A 1,500-token
# cap can therefore truncate a structured JSON response before the closing braces.
CASE_ASSISTANT_MAX_OUTPUT_TOKENS = int(
    os.environ.get("CW_ASSISTANT_MAX_OUTPUT_TOKENS", "6000")
)
CASE_ASSISTANT_RETRY_OUTPUT_TOKENS = int(
    os.environ.get("CW_ASSISTANT_RETRY_OUTPUT_TOKENS", "12000")
)
CASE_ASSISTANT_REASONING_EFFORT = os.environ.get(
    "CW_ASSISTANT_REASONING_EFFORT", "low"
).strip().lower()


def _assistant_clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + " …[truncated]"


def _extract_responses_output_text(payload: Dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: List[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and content.get("text"):
                chunks.append(str(content.get("text")))
            elif content.get("type") == "refusal" and content.get("refusal"):
                raise RuntimeError(str(content.get("refusal")))
    return "\n".join(chunks).strip()



def _extract_first_json_object(text: str) -> str:
    """Return the first balanced JSON object found in model text."""
    text = (text or "").lstrip("\ufeff").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start: idx + 1]
    return ""


def _parse_case_assistant_json(text: str) -> Dict[str, Any]:
    """Parse strict JSON plus common transport/display wrappers defensively."""
    candidates = [(text or "").strip()]
    extracted = _extract_first_json_object(text)
    if extracted and extracted not in candidates:
        candidates.append(extracted)
    last_error: Optional[Exception] = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            last_error = exc
    raise ValueError("No complete JSON object was found in the assistant output.") from last_error


def _model_supports_reasoning_effort(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def _response_incomplete_reason(body: Dict[str, Any]) -> str:
    details = body.get("incomplete_details") or {}
    if isinstance(details, dict):
        return str(details.get("reason") or "").strip()
    return ""

def is_production_deployment() -> bool:
    """Return True only when running in the production deployment.

    Default production detection is intentionally strict: the process must run
    from a directory named "prod" or CW_DEPLOY_ENV must be set to "prod".
    """
    if FORCE_TEST_INBOX:
        return False
    env = os.environ.get("CW_DEPLOY_ENV", "").strip().lower()
    if env == "prod":
        return True
    if env in {"dev", "development", "test", "debug"}:
        return False
    cwd_name = os.path.basename(os.path.abspath(os.getcwd()))
    file_dir_name = os.path.basename(os.path.abspath(os.path.dirname(__file__)))
    return cwd_name == PROD_DIR_NAME or file_dir_name == PROD_DIR_NAME


def extract_first_email(text: str) -> str:
    """Extract the first email address from free text."""
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    return m.group(0).strip().lower() if m else ""


def outbound_email_mode_label() -> str:
    return "PRODUCTION: sends to extracted business email" if is_production_deployment() else f"DEBUG: redirects to {TEST_INBOX_EMAIL}"


# Shared business-contact discovery. All Complaint Warrior modules can read the
# business_contacts table in CW_COMPANIES_DB (default: cw_companies.sqlite).
COMPANIES_DB_PATH = os.environ.get(
    "CW_COMPANIES_DB",
    os.environ.get("CW_SUBSCRIPTIONS_DB", "cw_companies.sqlite"),
)
CONTACT_LOOKUP_TTL_HOURS = float(os.environ.get("CW_CONTACT_LOOKUP_TTL_HOURS", "24"))
CONTACT_HTTP_TIMEOUT = float(os.environ.get("CW_CONTACT_HTTP_TIMEOUT", "12"))
CONTACT_MAX_HTML_BYTES = int(os.environ.get("CW_CONTACT_MAX_HTML_BYTES", "2000000"))
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "yahoo.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "mail.com", "gmx.com", "yandex.com",
}
CONTACT_PATH_WORDS = (
    "contact", "support", "help", "customer-service", "customer_service",
    "about", "locations", "store", "service",
)


def normalize_company_key(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    suffixes = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "plc"}
    words = [w for w in text.split() if w not in suffixes]
    return " ".join(words) or text


def extract_email_candidates(text: str, excluded: Optional[set[str]] = None) -> List[str]:
    excluded = {x.lower() for x in (excluded or set()) if x}
    values: List[str] = []
    for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or ""):
        email = email.strip(".,;:()[]<>\"'").lower()
        if email in excluded or email in values:
            continue
        values.append(email)
    return values


def normalize_phone(value: str) -> str:
    value = (value or "").strip()
    has_plus = value.startswith("+")
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if has_plus and 8 <= len(digits) <= 15:
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return digits
    return ""


def extract_phone_candidates(text: str, require_context: bool = True) -> List[str]:
    values: List[str] = []
    pattern = re.compile(
        r"(?<!\d)(?:\+?1[\s.()\-]*)?(?:\(?\d{3}\)?[\s.\-]*)\d{3}[\s.\-]*\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?!\d)",
        re.IGNORECASE,
    )
    context_words = ("phone", "call", "contact", "support", "customer", "service", "tel", "mobile", "office")
    for match in pattern.finditer(text or ""):
        if require_context:
            left = max(0, match.start() - 55)
            right = min(len(text), match.end() + 55)
            context = (text or "")[left:right].lower()
            if not any(word in context for word in context_words):
                continue
        normalized = normalize_phone(match.group(0))
        if normalized and normalized not in values:
            values.append(normalized)
    return values


def email_to_website(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].strip(" .")
    if not domain or domain in PERSONAL_EMAIL_DOMAINS:
        return ""
    return f"https://{domain}"


def _hostname_is_public(hostname: str) -> bool:
    hostname = (hostname or "").strip().lower().rstrip(".")
    if not hostname or hostname in {"localhost", "localhost.localdomain"}:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


class _ContactHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []
        self.text_parts: List[str] = []
        self.title_parts: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs or [])
        if tag.lower() == "a" and attrs_dict.get("href"):
            self.hrefs.append(attrs_dict["href"])
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        cleaned = " ".join((data or "").split())
        if cleaned:
            self.text_parts.append(cleaned)
            if self._in_title:
                self.title_parts.append(cleaned)


# Complaint-wide resolution/escalation statuses updated by external modules
# and/or by the main Streamlit UI. These keys are intentionally stable
# because they are persisted in cw_store.sqlite.
COMPLAINT_MODULE_STATUSES = {
    "resolved": "Resolved",
    "social_network_shared": "Social network shared",
    "charge_back_initiated": "Charge-back initiated",
    "submitted_to_small_claim_court": "Submitted to small claim court",
    "escalated_to_authorities": "Escalated to authorities",
}

# These statuses pause ordinary negotiation/recommendation loops.
# submitted_to_small_claim_court is terminal for app recommendations.
# escalated_to_authorities pauses until an inbound reply is logged after escalation.
ACTION_PAUSE_MODULE_STATUSES = {
    "submitted_to_small_claim_court",
    "escalated_to_authorities",
}





def _resolution_strategy_from_dict(raw: Optional[Dict[str, Any]]) -> ResolutionStrategy:
    """Build ResolutionStrategy while ignoring UI-only/runtime keys.

    ComplaintState.strategy is also used to persist recommendation metadata such as
    next_recommended_resolution_status. ResolutionStrategy does not accept those
    extra keys, so passing the whole dict causes:
        __init__() got an unexpected keyword argument 'next_recommended_resolution_status'
    """
    raw = raw or {}
    allowed = {
        "primary_goal": raw.get("primary_goal") or "general resolution",
        "acceptable_fallbacks": raw.get("acceptable_fallbacks") or [],
        "escalate_if": raw.get("escalate_if") or [],
        "evidence_needed": raw.get("evidence_needed") or [],
    }
    return ResolutionStrategy(**allowed)

def now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def decode_best_effort_text(payload) -> str:
    if not payload:
        return ""

    def decode_data(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    body = payload.get("body", {}) or {}
    if body.get("data"):
        return decode_data(body["data"])

    parts = payload.get("parts", []) or []
    for mime in ("text/plain", "text/html"):
        for p in parts:
            if p.get("mimeType") == mime:
                pdata = (p.get("body") or {}).get("data")
                if pdata:
                    txt = decode_data(pdata)
                    return re.sub(r"<[^>]+>", " ", txt) if mime == "text/html" else txt
    for p in parts:
        if p.get("parts"):
            t = decode_best_effort_text(p)
            if t:
                return t
    return ""


def get_header(headers, name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def build_gmail_service(token_db_path: str = "cw_gmail_tokens.sqlite", token_key: str = "default"):
    # Gmail auth flow preserved exactly: read token JSON from SQLite store created by gmail_oauth_server.py
    store = GmailTokenStore(db_path=token_db_path)
    token_json = store.get(token_key)
    if not token_json:
        raise RuntimeError("Gmail is not connected. No token found in SQLite. Open the Streamlit UI and click 'Connect Gmail' first.")

    creds = Credentials(
        token=token_json.get("token"),
        refresh_token=token_json.get("refresh_token"),
        token_uri=token_json.get("token_uri"),
        client_id=token_json.get("client_id"),
        client_secret=token_json.get("client_secret"),
        scopes=token_json.get("scopes"),
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_json["token"] = creds.token
            if getattr(creds, "refresh_token", None):
                token_json["refresh_token"] = creds.refresh_token
            store.set(token_key, token_json)
        else:
            raise RuntimeError("Gmail credentials are invalid and cannot be refreshed. Re-connect Gmail via OAuth.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def ensure_label(service, label_name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lb in labels:
        if lb.get("name") == label_name:
            return lb["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def add_label(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()


def get_thread(service, thread_id: str) -> dict:
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


def send_email_with_attachments(service, to_email: str, subject: str, body_text: str, attachments: Optional[List[str]] = None, thread_id: Optional[str] = None):
    attachments = attachments or []
    msg = MIMEMultipart()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    for path in attachments:
        if not path or not os.path.exists(path):
            continue
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)

        with open(path, "rb") as f:
            part = MIMEBase(maintype, subtype)
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
            msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return service.users().messages().send(userId="me", body=body).execute()


def build_evidence_pack_pdf(out_pdf_path: str, complaint_id: str, files: List[str]):
    c = rl_canvas.Canvas(out_pdf_path, pagesize=letter)
    _, height = letter
    y = height - 50

    def line(txt: str, dy: int = 14):
        nonlocal y
        c.drawString(50, y, txt[:140])
        y -= dy
        if y < 70:
            c.showPage()
            y = height - 50

    line("Complaint Evidence Pack (Summary)", 18)
    line(f"Complaint ID: {complaint_id}")
    line(f"Generated: {now_ts()}", 18)
    line("Included files:", 18)
    for p in files or []:
        if os.path.exists(p):
            line(f" - {os.path.basename(p)}")
    c.save()


def _empty_module_statuses() -> Dict[str, Dict[str, Any]]:
    return {
        key: {"done": False, "label": label, "updated_at": None, "note": ""}
        for key, label in COMPLAINT_MODULE_STATUSES.items()
    }

def _normalize_module_statuses(raw: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    statuses = _empty_module_statuses()
    for key, value in (raw or {}).items():
        if key not in statuses:
            continue
        if isinstance(value, dict):
            statuses[key].update(value)
            statuses[key]["label"] = COMPLAINT_MODULE_STATUSES[key]
        else:
            statuses[key]["done"] = bool(value)
    return statuses


@dataclass
class ActivityEvent:
    ts: str
    channel: str          # email | phone | system
    kind: str             # sent | received | decision | status | call | docs
    title: str
    detail: str
    meta: Dict[str, Any]

@dataclass
class ThreadState:
    thread_id: str
    label: str
    status: str = "open"
    stage: str = "initial_demand"
    last_handled_msg_id: Optional[str] = None
    last_decision: Optional[Dict[str, Any]] = None
    drafts: List[Dict[str, str]] = None
    satisfaction: Optional[Dict[str, Any]] = None

@dataclass
class ComplaintState:
    complaint_id: str
    subject: str
    complaint_raw: str
    complaint_professional: str
    user_email: str
    user_name: str
    company_name: str
    company_email: str
    company_phone: str
    company_website: str
    contact_discovery: Dict[str, Any]
    created_at: str
    docs: List[str]
    evidence_pack_pdf: Optional[str]
    strategy: Dict[str, Any]
    current_status_summary: str
    final_conclusion: str
    auto_send_policy: str
    module_statuses: Dict[str, Dict[str, Any]]
    threads: Dict[str, ThreadState]
    activities: List[Dict[str, Any]]

    def to_json(self) -> dict:
        return {
            "complaint_id": self.complaint_id,
            "subject": self.subject,
            "complaint_raw": self.complaint_raw,
            "complaint_professional": self.complaint_professional,
            "user_email": self.user_email,
            "user_name": self.user_name,
            "company_name": self.company_name,
            "company_email": self.company_email,
            "company_phone": self.company_phone,
            "company_website": self.company_website,
            "contact_discovery": self.contact_discovery,
            "created_at": self.created_at,
            "docs": self.docs,
            "evidence_pack_pdf": self.evidence_pack_pdf,
            "strategy": self.strategy,
            "current_status_summary": self.current_status_summary,
            "final_conclusion": self.final_conclusion,
            "auto_send_policy": self.auto_send_policy,
            "module_statuses": self.module_statuses,
            "threads": {k: asdict(v) for k, v in self.threads.items()},
            "activities": self.activities,
        }

    @staticmethod
    def from_json(d: dict) -> "ComplaintState":
        threads = {k: ThreadState(**v) for k, v in (d.get("threads") or {}).items()}
        return ComplaintState(
            complaint_id=d["complaint_id"],
            subject=d.get("subject",""),
            complaint_raw=d.get("complaint_raw",""),
            complaint_professional=d.get("complaint_professional",""),
            user_email=d.get("user_email",""),
            user_name=d.get("user_name",""),
            company_name=d.get("company_name",""),
            company_email=d.get("company_email",""),
            company_phone=d.get("company_phone", ""),
            company_website=d.get("company_website", ""),
            contact_discovery=d.get("contact_discovery") or {},
            created_at=d.get("created_at", now_ts()),
            docs=d.get("docs") or [],
            evidence_pack_pdf=d.get("evidence_pack_pdf"),
            strategy=d.get("strategy") or {},
            current_status_summary=d.get("current_status_summary","Complaint created."),
            final_conclusion=d.get("final_conclusion",""),
            auto_send_policy=d.get("auto_send_policy", DEFAULT_AUTO_SEND_POLICY),
            module_statuses=_normalize_module_statuses(d.get("module_statuses")),
            threads=threads,
            activities=d.get("activities") or [],
        )


class ComplaintWarriorManager:
    def __init__(self, text_processor: TextProcessing, log_cb: Optional[Callable[[str], None]] = None, gmail_user_key: str = "default", token_db_path: str = "cw_gmail_tokens.sqlite", complaint_db_path: Optional[str] = None):
        self.tp = text_processor
        self.log_cb = log_cb or (lambda s: None)
        self.token_db_path = token_db_path
        self.gmail_user_key = gmail_user_key
        self.service = None #build_gmail_service(token_db_path=self.token_db_path, token_key=self.gmail_user_key)
        self.processed_label_id = None # ensure_label(self.service, LABEL_PROCESSED)

        self.user_email: Optional[str] = None
        self.complaint_db_path = complaint_db_path or os.environ.get("CW_DB_PATH", "cw_store.sqlite")
        self.companies_db_path = COMPANIES_DB_PATH
        self.store = ComplaintStore(db_path=self.complaint_db_path)
        self.call_store = CallResultStore(db_path=self.complaint_db_path)
        self.complaints: Dict[str, ComplaintState] = {}

    def _ensure_gmail(self):
        if self.service is None:
            self.service = build_gmail_service(
                token_db_path=self.token_db_path,
                token_key=self.gmail_user_key,
            )
            self.processed_label_id = ensure_label(self.service, LABEL_PROCESSED)

    def _log(self, msg: str):
        if self.log_cb:
            self.log_cb(msg)

    def set_user(self, user_email: str):
        user_email = (user_email or "").strip().lower()
        if not user_email:
            raise ValueError("user_email is required")
        self.user_email = user_email
        raw = self.store.load_all(user_email)
        self.complaints = {cid: ComplaintState.from_json(js) for cid, js in raw.items()}
        self._log(f"[manager] active app user={user_email}, complaints_loaded={len(self.complaints)}")

    def set_gmail_user(self, gmail_user_key: str):
        gmail_user_key = (gmail_user_key or "").strip()
        if not gmail_user_key:
            raise ValueError("gmail_user_key is required")
        self.gmail_user_key = gmail_user_key
        self.service = build_gmail_service(token_db_path=self.token_db_path, token_key=self.gmail_user_key)
        self.processed_label_id = ensure_label(self.service, LABEL_PROCESSED)
        self._log(f"[manager] switched gmail token key to {gmail_user_key}")

    def _require_user(self) -> str:
        if not self.user_email:
            raise RuntimeError("No active app user. Set user email first.")
        return self.user_email

    def _save(self, cs: ComplaintState):
        self.store.upsert(self._require_user(), cs.complaint_id, cs.to_json())

    def _append_activity(self, cs: ComplaintState, channel: str, kind: str, title: str, detail: str, meta: Optional[Dict[str, Any]] = None):
        cs.activities.append(asdict(ActivityEvent(
            ts=now_ts(),
            channel=channel,
            kind=kind,
            title=title,
            detail=detail,
            meta=meta or {},
        )))
        self._save(cs)

    def list_complaints(self) -> List[ComplaintState]:
        return list(self.complaints.values())

    def get_complaint(self, complaint_id: str) -> ComplaintState:
        return self.complaints[complaint_id]

    def case_assistant_config(self) -> Dict[str, Any]:
        api_key = (
            os.environ.get("CW_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        return {
            "configured": bool(api_key),
            "model": CASE_ASSISTANT_MODEL,
            "api_url": CASE_ASSISTANT_API_URL,
        }

    def build_case_assistant_context(
        self,
        selected_complaint_id: Optional[str] = None,
        *,
        max_activities_per_case: int = 24,
    ) -> Dict[str, Any]:
        """Return a bounded, active-user-only case snapshot for the assistant."""
        active_user = self._require_user()
        cases: List[Dict[str, Any]] = []
        total_chars = 0
        ordered = sorted(
            self.complaints.values(),
            key=lambda c: (c.created_at or "", c.complaint_id),
            reverse=True,
        )

        for cs in ordered:
            statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))
            simplified_statuses = {
                key: {
                    "done": bool(value.get("done")),
                    "label": value.get("label") or COMPLAINT_MODULE_STATUSES.get(key, key),
                    "updated_at": value.get("updated_at"),
                    "note": _assistant_clip(value.get("note"), 500),
                }
                for key, value in statuses.items()
            }

            threads: List[Dict[str, Any]] = []
            for thread_id, ts in (cs.threads or {}).items():
                satisfaction = ts.satisfaction or {}
                threads.append({
                    "thread_id": thread_id,
                    "label": ts.label,
                    "status": ts.status,
                    "stage": ts.stage,
                    "draft_count": len(ts.drafts or []),
                    "satisfaction": {
                        "verdict": satisfaction.get("verdict"),
                        "reason": _assistant_clip(satisfaction.get("reason"), 700),
                    } if satisfaction else None,
                })

            recent_activities: List[Dict[str, Any]] = []
            for event in (cs.activities or [])[-max_activities_per_case:]:
                if not isinstance(event, dict):
                    continue
                recent_activities.append({
                    "ts": event.get("ts"),
                    "channel": event.get("channel"),
                    "kind": event.get("kind"),
                    "title": _assistant_clip(event.get("title"), 250),
                    "detail": _assistant_clip(event.get("detail"), 1200),
                    "meta": {
                        str(k): _assistant_clip(v, 400)
                        for k, v in (event.get("meta") or {}).items()
                        if k in {
                            "stage", "verdict", "subject", "confidence",
                            "status_key", "done", "phone", "from", "sender",
                            "intended_recipient", "actual_recipient",
                        }
                    },
                })

            case = {
                "complaint_id": cs.complaint_id,
                "is_selected": cs.complaint_id == selected_complaint_id,
                "subject": _assistant_clip(cs.subject, 500),
                "company": {
                    "name": _assistant_clip(cs.company_name, 300),
                    "email": _assistant_clip(cs.company_email, 300),
                    "phone": _assistant_clip(cs.company_phone, 100),
                    "website": _assistant_clip(cs.company_website, 400),
                },
                "consumer_name": _assistant_clip(cs.user_name, 200),
                "created_at": cs.created_at,
                "complaint_original": _assistant_clip(cs.complaint_raw, 2600),
                "complaint_professional": _assistant_clip(cs.complaint_professional, 2600),
                "current_status": _assistant_clip(cs.current_status_summary, 900),
                "final_conclusion": _assistant_clip(cs.final_conclusion, 900),
                "communication_policy": cs.auto_send_policy,
                "strategy": {
                    "primary_goal": _assistant_clip((cs.strategy or {}).get("primary_goal"), 700),
                    "acceptable_fallbacks": (cs.strategy or {}).get("acceptable_fallbacks") or [],
                    "escalate_if": (cs.strategy or {}).get("escalate_if") or [],
                    "evidence_needed": (cs.strategy or {}).get("evidence_needed") or [],
                    "next_recommended_resolution_status": (cs.strategy or {}).get(
                        "next_recommended_resolution_status"
                    ),
                },
                "module_statuses": simplified_statuses,
                "documents": [os.path.basename(str(p)) for p in (cs.docs or [])],
                "evidence_pack": os.path.basename(cs.evidence_pack_pdf) if cs.evidence_pack_pdf else "",
                "threads": threads,
                "recent_activities": recent_activities,
            }
            case_chars = len(json.dumps(case, ensure_ascii=False, default=str))
            if cases and total_chars + case_chars > CASE_ASSISTANT_MAX_CONTEXT_CHARS:
                # Preserve all case identities even when detailed history must be shortened.
                case = {
                    "complaint_id": cs.complaint_id,
                    "is_selected": cs.complaint_id == selected_complaint_id,
                    "subject": _assistant_clip(cs.subject, 350),
                    "company": {"name": _assistant_clip(cs.company_name, 250)},
                    "current_status": _assistant_clip(cs.current_status_summary, 600),
                    "final_conclusion": _assistant_clip(cs.final_conclusion, 600),
                    "module_statuses": simplified_statuses,
                    "context_note": "Detailed history omitted because the active user's context limit was reached.",
                }
                case_chars = len(json.dumps(case, ensure_ascii=False, default=str))
            cases.append(case)
            total_chars += case_chars

        return {
            "active_user": active_user,
            "selected_complaint_id": selected_complaint_id or "",
            "complaint_count": len(cases),
            "complaints": cases,
        }

    def ask_case_assistant(
        self,
        question: str,
        *,
        control_guide: List[Dict[str, Any]],
        allowed_navigation_targets: List[str],
        selected_complaint_id: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Ask the Responses API about the active user's cases and UI controls."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Enter a question for the case assistant.")
        if requests is None:
            raise RuntimeError("The requests package is unavailable.")

        config = self.case_assistant_config()
        if not config["configured"]:
            raise RuntimeError(
                "ChatGPT is not configured. Set OPENAI_API_KEY or CW_OPENAI_API_KEY on the server."
            )
        api_key = (
            os.environ.get("CW_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        context = self.build_case_assistant_context(selected_complaint_id)
        history = []
        for item in (conversation_history or [])[-8:]:
            role = item.get("role")
            content = _assistant_clip(item.get("content"), 1800)
            if role in {"user", "assistant"} and content:
                history.append({"role": role, "content": content})

        instructions = (
            "You are the Complaint Warrior case and interface assistant. "
            "Answer questions using only ACTIVE_USER_CASE_CONTEXT and UI_CONTROL_GUIDE. "
            "The case context contains complaints belonging only to the authenticated active user. "
            "Do not invent facts, payments, replies, deadlines, legal filings, or company promises. "
            "When a fact is missing, say that it is not recorded. Refer to cases by complaint_id. "
            "You may explain likely next steps but must distinguish general information from legal advice. "
            "Never claim that you sent an email, called a company, deleted a complaint, changed status, "
            "filed in court, or completed an external action. This chat only answers and navigates. "
            "For each useful interface destination, return an action. Use the most specific target. "
            "Set auto_navigate=true only when the user explicitly asks to go, take me, show me, or open a section. "
            "Use an empty complaint_id for controls that are not case-specific. "
            "Keep the answer practical and concise. Return JSON matching the supplied schema."
        )

        user_payload = {
            "question": question,
            "recent_conversation": history,
            "ACTIVE_USER_CASE_CONTEXT": context,
            "UI_CONTROL_GUIDE": control_guide,
        }

        action_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string", "enum": allowed_navigation_targets},
                "label": {"type": "string"},
                "complaint_id": {"type": "string"},
                "reason": {"type": "string"},
                "auto_navigate": {"type": "boolean"},
            },
            "required": ["target", "label", "complaint_id", "reason", "auto_navigate"],
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "answer": {"type": "string"},
                "cited_complaint_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "actions": {
                    "type": "array",
                    "items": action_schema,
                },
                "requires_human_review": {"type": "boolean"},
            },
            "required": [
                "answer", "cited_complaint_ids", "actions", "requires_human_review"
            ],
        }
        base_payload = {
            "model": CASE_ASSISTANT_MODEL,
            "instructions": instructions,
            "input": json.dumps(user_payload, ensure_ascii=False, default=str),
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "complaint_warrior_case_assistant",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        if _model_supports_reasoning_effort(CASE_ASSISTANT_MODEL):
            base_payload["reasoning"] = {
                "effort": CASE_ASSISTANT_REASONING_EFFORT or "low"
            }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        def post_response(payload_to_send: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
            response = requests.post(
                CASE_ASSISTANT_API_URL,
                headers=headers,
                json=payload_to_send,
                timeout=CASE_ASSISTANT_TIMEOUT,
            )
            request_id_local = response.headers.get("x-request-id", "")
            try:
                response.raise_for_status()
            except Exception as exc:
                detail = _assistant_clip(response.text, 1200)
                raise RuntimeError(
                    f"OpenAI Responses API error ({response.status_code}): {detail or exc}"
                ) from exc
            try:
                body_local = response.json()
            except Exception as exc:
                raise RuntimeError("OpenAI returned a non-JSON HTTP response.") from exc
            if not isinstance(body_local, dict):
                raise RuntimeError("OpenAI returned an unexpected response object.")
            return body_local, request_id_local

        result: Optional[Dict[str, Any]] = None
        body: Dict[str, Any] = {}
        request_id = ""
        structured_errors: List[str] = []

        # Two structured attempts. The retry receives a larger output budget.
        for attempt, token_budget in enumerate(
            (CASE_ASSISTANT_MAX_OUTPUT_TOKENS, CASE_ASSISTANT_RETRY_OUTPUT_TOKENS),
            start=1,
        ):
            payload = dict(base_payload)
            payload["max_output_tokens"] = max(2000, int(token_budget))
            if attempt == 2:
                payload["instructions"] = (
                    instructions
                    + " A prior attempt was incomplete or malformed. Return one complete JSON "
                      "object only, with all required fields and no markdown fences."
                )
            body, request_id = post_response(payload)
            status = str(body.get("status") or "").strip().lower()
            incomplete_reason = _response_incomplete_reason(body)
            output_text = _extract_responses_output_text(body)
            if status == "incomplete":
                structured_errors.append(
                    f"attempt {attempt}: incomplete ({incomplete_reason or 'unknown reason'})"
                )
                continue
            if not output_text:
                structured_errors.append(f"attempt {attempt}: no text output")
                continue
            try:
                result = _parse_case_assistant_json(output_text)
                break
            except Exception as exc:
                structured_errors.append(
                    f"attempt {attempt}: malformed JSON ({_assistant_clip(exc, 180)})"
                )

        # Last-resort answer path: do not fail the user merely because navigation
        # metadata could not be parsed. Ask for concise text and derive a safe link.
        if result is None:
            fallback_instructions = (
                "You are the Complaint Warrior case assistant. Answer the user's question "
                "using only ACTIVE_USER_CASE_CONTEXT. Give a concise, practical answer. "
                "Identify cases by complaint_id. Do not claim that you performed any action. "
                "Do not return JSON or markdown code fences."
            )
            fallback_payload: Dict[str, Any] = {
                "model": CASE_ASSISTANT_MODEL,
                "instructions": fallback_instructions,
                "input": json.dumps(user_payload, ensure_ascii=False, default=str),
                "max_output_tokens": max(3000, CASE_ASSISTANT_MAX_OUTPUT_TOKENS),
                "text": {"verbosity": "low", "format": {"type": "text"}},
            }
            if _model_supports_reasoning_effort(CASE_ASSISTANT_MODEL):
                fallback_payload["reasoning"] = {
                    "effort": CASE_ASSISTANT_REASONING_EFFORT or "low"
                }
            body, fallback_request_id = post_response(fallback_payload)
            request_id = fallback_request_id or request_id
            fallback_text = _extract_responses_output_text(body)
            if not fallback_text:
                diagnostic = "; ".join(structured_errors) or "unknown structured-output failure"
                raise RuntimeError(
                    "The case assistant returned no usable answer after retry. "
                    f"Diagnostic: {diagnostic}. Request ID: {request_id or 'unavailable'}."
                )
            lowered_question = question.lower()
            fallback_actions: List[Dict[str, Any]] = []
            target = ""
            label = ""
            if "small claim" in lowered_question or "court" in lowered_question:
                target, label = "small_claims", "Open Small Claim Court Warrior"
            elif "chargeback" in lowered_question or "charge back" in lowered_question:
                target, label = "chargeback", "Open Charge Back Initiator"
            elif "regulator" in lowered_question or "authority" in lowered_question:
                target, label = "regulatory", "Open CW Regulatory"
            elif "phone" in lowered_question or "call" in lowered_question:
                target, label = "phone", "Open phone escalation"
            elif "email" in lowered_question or "negot" in lowered_question:
                target, label = "negotiation", "Open negotiation and email"
            if target in allowed_navigation_targets:
                fallback_actions.append({
                    "target": target,
                    "label": label,
                    "complaint_id": "",
                    "reason": "Open the relevant Complaint Warrior workflow.",
                    "auto_navigate": False,
                })
            result = {
                "answer": fallback_text,
                "cited_complaint_ids": [
                    cid for cid in self.complaints
                    if cid.lower() in fallback_text.lower()
                ],
                "actions": fallback_actions,
                "requires_human_review": True,
            }
            self._log(
                "[case_assistant] structured response fallback used: "
                + ("; ".join(structured_errors) or "unknown reason")
            )

        if not isinstance(result, dict):
            raise RuntimeError("The assistant returned an unexpected response type.")

        valid_case_ids = set(self.complaints.keys())
        safe_actions: List[Dict[str, Any]] = []
        for action in result.get("actions") or []:
            if not isinstance(action, dict):
                continue
            target = action.get("target")
            if target not in allowed_navigation_targets:
                continue
            complaint_id = (action.get("complaint_id") or "").strip()
            if complaint_id and complaint_id not in valid_case_ids:
                complaint_id = ""
            safe_actions.append({
                "target": target,
                "label": _assistant_clip(action.get("label"), 100) or "Open section",
                "complaint_id": complaint_id,
                "reason": _assistant_clip(action.get("reason"), 500),
                "auto_navigate": bool(action.get("auto_navigate")),
            })

        cited_ids = [
            cid for cid in (result.get("cited_complaint_ids") or [])
            if cid in valid_case_ids
        ]
        return {
            "answer": _assistant_clip(result.get("answer"), 7000),
            "cited_complaint_ids": list(dict.fromkeys(cited_ids)),
            "actions": safe_actions[:4],
            "requires_human_review": bool(result.get("requires_human_review")),
            "model": body.get("model") or CASE_ASSISTANT_MODEL,
            "request_id": request_id,
        }

    # -------------------- shared business-contact discovery --------------------
    def _ensure_business_contacts_table(self) -> None:
        with sqlite3.connect(self.companies_db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS business_contacts (
                    company_key TEXT PRIMARY KEY,
                    company_name TEXT NOT NULL,
                    contact_email TEXT,
                    contact_phone TEXT,
                    website TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    source_json TEXT,
                    first_seen_at REAL NOT NULL,
                    last_verified_at REAL,
                    last_used_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_business_contacts_name ON business_contacts(company_name)"
            )

    def _load_shared_business_contact(self, company_name: str) -> Dict[str, Any]:
        key = normalize_company_key(company_name)
        if not key:
            return {}
        try:
            self._ensure_business_contacts_table()
            with sqlite3.connect(self.companies_db_path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(
                    "SELECT * FROM business_contacts WHERE company_key=? LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    con.execute(
                        "UPDATE business_contacts SET last_used_at=? WHERE company_key=?",
                        (time.time(), key),
                    )
                    result = dict(row)
                    try:
                        result["sources"] = json.loads(result.get("source_json") or "[]")
                    except Exception:
                        result["sources"] = []
                    return result

                tables = {
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "subscribed_companies" not in tables:
                    return {}
                rows = con.execute(
                    """
                    SELECT company_name, contact_email, contact_phone
                    FROM subscribed_companies
                    WHERE is_active=1
                    """
                ).fetchall()
                for subscribed in rows:
                    if normalize_company_key(subscribed["company_name"] or "") != key:
                        continue
                    return {
                        "company_name": subscribed["company_name"] or company_name,
                        "contact_email": subscribed["contact_email"] or "",
                        "contact_phone": subscribed["contact_phone"] or "",
                        "website": email_to_website(subscribed["contact_email"] or ""),
                        "confidence": 0.95,
                        "sources": [{"source": "subscribed_companies"}],
                    }
                return {}
        except Exception as exc:
            self._log(f"[contacts] shared lookup failed for {company_name}: {exc}")
            return {}

    def _upsert_shared_business_contact(self, company_name: str, contact: Dict[str, Any]) -> None:
        key = normalize_company_key(company_name)
        if not key or not any(contact.get(k) for k in ("email", "phone", "website")):
            return
        now = time.time()
        source_item = {
            "source": contact.get("source") or "complaint_warrior",
            "source_url": contact.get("source_url") or "",
            "observed_at": now_ts(),
        }
        try:
            self._ensure_business_contacts_table()
            with sqlite3.connect(self.companies_db_path) as con:
                con.row_factory = sqlite3.Row
                old = con.execute(
                    "SELECT * FROM business_contacts WHERE company_key=? LIMIT 1",
                    (key,),
                ).fetchone()
                sources: List[Dict[str, Any]] = []
                first_seen = now
                old_email = old_phone = old_website = ""
                old_confidence = 0.0
                if old:
                    old_d = dict(old)
                    first_seen = float(old_d.get("first_seen_at") or now)
                    old_email = old_d.get("contact_email") or ""
                    old_phone = old_d.get("contact_phone") or ""
                    old_website = old_d.get("website") or ""
                    old_confidence = float(old_d.get("confidence") or 0.0)
                    try:
                        sources = json.loads(old_d.get("source_json") or "[]")
                    except Exception:
                        sources = []
                if source_item not in sources:
                    sources.append(source_item)
                sources = sources[-20:]
                con.execute(
                    """
                    INSERT INTO business_contacts (
                        company_key, company_name, contact_email, contact_phone,
                        website, confidence, source_json, first_seen_at,
                        last_verified_at, last_used_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_key) DO UPDATE SET
                        company_name=excluded.company_name,
                        contact_email=CASE WHEN excluded.contact_email<>'' THEN excluded.contact_email ELSE business_contacts.contact_email END,
                        contact_phone=CASE WHEN excluded.contact_phone<>'' THEN excluded.contact_phone ELSE business_contacts.contact_phone END,
                        website=CASE WHEN excluded.website<>'' THEN excluded.website ELSE business_contacts.website END,
                        confidence=MAX(business_contacts.confidence, excluded.confidence),
                        source_json=excluded.source_json,
                        last_verified_at=excluded.last_verified_at,
                        last_used_at=excluded.last_used_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        key,
                        company_name.strip(),
                        contact.get("email") or old_email,
                        contact.get("phone") or old_phone,
                        contact.get("website") or old_website,
                        max(old_confidence, float(contact.get("confidence") or 0.0)),
                        json.dumps(sources, ensure_ascii=False),
                        first_seen,
                        now,
                        now,
                        now,
                    ),
                )
                # Enrich a subscribed-company record if it already exists. Never
                # insert here, because that would incorrectly create a subscription.
                tables = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "subscribed_companies" in tables:
                    con.execute(
                        """
                        UPDATE subscribed_companies
                        SET contact_email=CASE WHEN COALESCE(contact_email, '')='' THEN ? ELSE contact_email END,
                            contact_phone=CASE WHEN COALESCE(contact_phone, '')='' THEN ? ELSE contact_phone END,
                            updated_at=?
                        WHERE lower(trim(company_name))=lower(trim(?))
                        """,
                        (contact.get("email") or "", contact.get("phone") or "", now, company_name),
                    )
        except Exception as exc:
            self._log(f"[contacts] shared save failed for {company_name}: {exc}")

    def _complaint_contact_text(self, cs: ComplaintState) -> str:
        blocks = [
            cs.subject or "",
            cs.complaint_raw or "",
            cs.complaint_professional or "",
            cs.current_status_summary or "",
            cs.final_conclusion or "",
        ]
        for event in cs.activities or []:
            blocks.append(str(event.get("title") or ""))
            blocks.append(str(event.get("detail") or ""))
            meta = event.get("meta") or {}
            if isinstance(meta, dict):
                for key in ("intended_recipient", "actual_recipient", "from", "sender", "phone"):
                    if meta.get(key):
                        blocks.append(str(meta[key]))
        # Unsaved/generated drafts are intentionally excluded: an LLM-generated
        # address or phone number is not evidence of a real business contact.
        return "\n".join(blocks)

    def _merge_contact_into_complaint(
        self,
        cs: ComplaintState,
        contact: Dict[str, Any],
        source: str,
        source_url: str = "",
        confidence: float = 0.5,
    ) -> List[str]:
        """Fill only blank contact fields; never replace a stored/user value."""
        changed: List[str] = []
        email = (contact.get("email") or contact.get("contact_email") or "").strip().lower()
        phone = normalize_phone(contact.get("phone") or contact.get("contact_phone") or "")
        website = (contact.get("website") or "").strip()
        if email and email != (cs.user_email or "").strip().lower() and not cs.company_email:
            cs.company_email = email
            changed.append("email")
        if phone and not cs.company_phone:
            cs.company_phone = phone
            changed.append("phone")
        if website and not cs.company_website:
            cs.company_website = website
            changed.append("website")
        if changed:
            cs.contact_discovery = cs.contact_discovery or {}
            history = cs.contact_discovery.get("history")
            if not isinstance(history, list):
                history = []
            history.append({
                "ts": now_ts(),
                "source": source,
                "source_url": source_url,
                "confidence": confidence,
                "fields": changed,
            })
            cs.contact_discovery["history"] = history[-20:]
            cs.contact_discovery["last_source"] = source
            cs.contact_discovery["last_source_url"] = source_url
            cs.contact_discovery["last_confidence"] = confidence
            field_sources = cs.contact_discovery.get("field_sources")
            if not isinstance(field_sources, dict):
                field_sources = {}
            for field in changed:
                field_sources[field] = source
            cs.contact_discovery["field_sources"] = field_sources
        return changed

    def _learn_contacts_from_text(
        self,
        cs: ComplaintState,
        text: str,
        source: str,
        source_url: str = "",
        require_phone_context: bool = True,
    ) -> List[str]:
        excluded = {
            (cs.user_email or "").strip().lower(),
            TEST_INBOX_EMAIL.lower(),
        }
        emails = extract_email_candidates(text, excluded=excluded)
        phones = extract_phone_candidates(text, require_context=require_phone_context)
        contact: Dict[str, Any] = {}
        if emails:
            contact["email"] = emails[0]
            contact["website"] = email_to_website(emails[0])
        if phones:
            contact["phone"] = phones[0]
        return self._merge_contact_into_complaint(
            cs,
            contact,
            source=source,
            source_url=source_url,
            confidence=0.72 if source.startswith("communication") else 0.58,
        )

    def _public_get(self, url: str) -> Optional[Any]:
        if requests is None:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not _hostname_is_public(parsed.hostname or ""):
            return None
        try:
            response = requests.get(
                url,
                timeout=CONTACT_HTTP_TIMEOUT,
                allow_redirects=True,
                headers={"User-Agent": "ComplaintWarriorContactResolver/1.0 (+public business contact lookup)"},
                stream=True,
            )
            response.raise_for_status()
            final = urlparse(response.url)
            if final.scheme not in {"http", "https"} or not _hostname_is_public(final.hostname or ""):
                return None
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                return None
            chunks = []
            total = 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > CONTACT_MAX_HTML_BYTES:
                    break
                chunks.append(chunk)
            response._cw_limited_content = b"".join(chunks)
            return response
        except Exception as exc:
            self._log(f"[contacts] website fetch failed {url}: {exc}")
            return None

    def _crawl_website_contacts(self, website: str, company_name: str) -> Dict[str, Any]:
        if not website:
            return {}
        if not re.match(r"^https?://", website, flags=re.IGNORECASE):
            website = "https://" + website.lstrip("/")
        start = urlparse(website)
        if not start.hostname:
            return {}
        queue = [website]
        visited: set[str] = set()
        best: Dict[str, Any] = {"website": website, "source": "official_website", "source_url": website, "confidence": 0.78}
        excluded = {TEST_INBOX_EMAIL.lower()}
        while queue and len(visited) < 6:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            response = self._public_get(url)
            if response is None:
                continue
            raw = getattr(response, "_cw_limited_content", b"")
            encoding = response.encoding or "utf-8"
            html_text = raw.decode(encoding, errors="replace")
            parser = _ContactHTMLParser()
            try:
                parser.feed(html_text)
            except Exception:
                pass
            visible = "\n".join(parser.text_parts)
            # mailto:/tel: links are the most reliable signals.
            link_emails: List[str] = []
            link_phones: List[str] = []
            for href in parser.hrefs:
                href_s = (href or "").strip()
                if href_s.lower().startswith("mailto:"):
                    link_emails.extend(extract_email_candidates(href_s[7:].split("?", 1)[0], excluded))
                elif href_s.lower().startswith("tel:"):
                    phone = normalize_phone(href_s[4:].split("?", 1)[0])
                    if phone:
                        link_phones.append(phone)
                else:
                    candidate = urljoin(response.url, href_s)
                    cp = urlparse(candidate)
                    if cp.hostname and cp.hostname.lower().lstrip("www.") == start.hostname.lower().lstrip("www."):
                        path_lower = (cp.path or "").lower()
                        if any(word in path_lower for word in CONTACT_PATH_WORDS) and candidate not in visited and candidate not in queue:
                            queue.append(candidate)
            emails = link_emails or extract_email_candidates(visible, excluded)
            phones = link_phones or extract_phone_candidates(visible, require_context=True)
            if emails and not best.get("email"):
                # Prefer addresses on the same domain as the official website.
                domain = (urlparse(response.url).hostname or "").lower().lstrip("www.")
                same_domain = [e for e in emails if e.rsplit("@", 1)[-1].lower().lstrip("www.") == domain]
                best["email"] = (same_domain or emails)[0]
                best["source_url"] = response.url
            if phones and not best.get("phone"):
                best["phone"] = phones[0]
                best["source_url"] = response.url
            if best.get("email") and best.get("phone"):
                break
        return best

    def _company_location_hint(self, cs: ComplaintState) -> str:
        text = f"{cs.subject}\n{cs.complaint_raw}"
        match = re.search(r"\b([A-Za-z][A-Za-z .'-]{1,40}),\s*(CA|California)\b", text, re.IGNORECASE)
        if match:
            return f"{match.group(1).strip()}, CA"
        zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
        return zip_match.group(1) if zip_match else ""

    def _google_places_contact_lookup(self, cs: ComplaintState) -> Dict[str, Any]:
        api_key = os.environ.get("CW_GOOGLE_PLACES_API_KEY", "").strip()
        if not api_key or requests is None or not cs.company_name.strip():
            return {}
        location = self._company_location_hint(cs)
        query = " ".join(x for x in [cs.company_name.strip(), location] if x)
        field_mask = ",".join([
            "places.id", "places.displayName", "places.formattedAddress",
            "places.nationalPhoneNumber", "places.internationalPhoneNumber",
            "places.websiteUri", "places.googleMapsUri", "places.businessStatus",
        ])
        try:
            response = requests.post(
                "https://places.googleapis.com/v1/places:searchText",
                json={"textQuery": query, "maxResultCount": 5, "languageCode": "en", "regionCode": "US"},
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": field_mask,
                },
                timeout=CONTACT_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            places = response.json().get("places") or []
        except Exception as exc:
            self._log(f"[contacts] Google Places lookup failed for {query}: {exc}")
            return {}
        if not places:
            return {}
        wanted = normalize_company_key(cs.company_name)
        def place_score(place: Dict[str, Any]) -> float:
            name_obj = place.get("displayName") or {}
            name = name_obj.get("text") if isinstance(name_obj, dict) else str(name_obj)
            normalized = normalize_company_key(name)
            ratio = SequenceMatcher(None, wanted, normalized).ratio() if wanted and normalized else 0.0
            token_overlap = len(set(wanted.split()) & set(normalized.split())) / max(1, len(set(wanted.split())))
            return ratio + token_overlap
        place = max(places, key=place_score)
        name_obj = place.get("displayName") or {}
        display_name = name_obj.get("text") if isinstance(name_obj, dict) else str(name_obj)
        return {
            "email": "",
            "phone": place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber") or "",
            "website": place.get("websiteUri") or "",
            "source": "google_places",
            "source_url": place.get("googleMapsUri") or "",
            "confidence": min(0.96, 0.70 + 0.20 * place_score(place)),
            "display_name": display_name,
            "address": place.get("formattedAddress") or "",
            "place_id": place.get("id") or "",
        }

    def _custom_contact_lookup(self, cs: ComplaintState) -> Dict[str, Any]:
        endpoint = os.environ.get("CW_CONTACT_LOOKUP_API_URL", "").strip()
        token = os.environ.get("CW_CONTACT_LOOKUP_API_TOKEN", "").strip()
        if not endpoint or requests is None or not cs.company_name.strip():
            return {}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json={
                    "company_name": cs.company_name,
                    "known_email": cs.company_email,
                    "known_phone": cs.company_phone,
                    "known_website": cs.company_website,
                    "location_hint": self._company_location_hint(cs),
                    "complaint_context": (cs.complaint_raw or cs.complaint_professional or "")[:2000],
                },
                timeout=max(CONTACT_HTTP_TIMEOUT, 20),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return {}
            return {
                "email": data.get("email") or data.get("contact_email") or "",
                "phone": data.get("phone") or data.get("contact_phone") or "",
                "website": data.get("website") or "",
                "source": data.get("source") or "contact_lookup_api",
                "source_url": data.get("source_url") or "",
                "confidence": float(data.get("confidence") or 0.65),
            }
        except Exception as exc:
            self._log(f"[contacts] custom lookup failed for {cs.company_name}: {exc}")
            return {}

    def _contact_result(
        self,
        cs: ComplaintState,
        *,
        require_email: bool,
        require_phone: bool,
        changed_sources: Optional[List[str]] = None,
        discovery_skipped: bool = False,
    ) -> Dict[str, Any]:
        field_sources = (cs.contact_discovery or {}).get("field_sources") or {}
        return {
            "company_name": cs.company_name,
            "email": cs.company_email,
            "phone": cs.company_phone,
            "website": cs.company_website,
            "email_found": bool(cs.company_email),
            "phone_found": bool(cs.company_phone),
            "required_email_missing": bool(require_email and not cs.company_email),
            "required_phone_missing": bool(require_phone and not cs.company_phone),
            "sources": list(dict.fromkeys(changed_sources or [])),
            "field_sources": dict(field_sources),
            "email_source": field_sources.get("email", "stored" if cs.company_email else ""),
            "phone_source": field_sources.get("phone", "stored" if cs.company_phone else ""),
            "website_source": field_sources.get("website", "stored" if cs.company_website else ""),
            "discovery_skipped": bool(discovery_skipped),
            "external_lookup_available": bool(
                os.environ.get("CW_GOOGLE_PLACES_API_KEY")
                or os.environ.get("CW_CONTACT_LOOKUP_API_URL")
                or cs.company_website
                or email_to_website(cs.company_email)
            ),
        }

    def ensure_business_contacts(
        self,
        complaint_id: str,
        *,
        require_email: bool = False,
        require_phone: bool = False,
        force_external: bool = False,
    ) -> Dict[str, Any]:
        """Resolve missing public business contacts and persist them for all warriors.

        Resolution order:
        1. Current complaint fields and all complaint communications.
        2. Shared cw_companies.sqlite/business_contacts cache.
        3. Official website inferred from a known business email domain.
        4. Google Places Text Search (when CW_GOOGLE_PLACES_API_KEY is set).
        5. Optional organization-specific resolver endpoint.
        6. Official website crawl using the website returned by a provider.
        """
        cs = self.complaints[complaint_id]
        cs.contact_discovery = cs.contact_discovery or {}
        field_sources = cs.contact_discovery.get("field_sources")
        if not isinstance(field_sources, dict):
            field_sources = {}
        cs.contact_discovery["field_sources"] = field_sources

        # Action-specific safeguard: once the contact required by this action is
        # already stored, do not run communication/shared/external discovery.
        # This guarantees that a user-entered phone is the phone used for calls,
        # and a user-entered email is the email used for drafting/sending.
        required_satisfied = (
            (not require_email or bool(cs.company_email))
            and (not require_phone or bool(cs.company_phone))
        )
        if (require_email or require_phone) and required_satisfied and not force_external:
            if cs.company_name and any((cs.company_email, cs.company_phone, cs.company_website)):
                self._upsert_shared_business_contact(cs.company_name, {
                    "email": cs.company_email,
                    "phone": cs.company_phone,
                    "website": cs.company_website,
                    "source": field_sources.get("email") or field_sources.get("phone") or "stored_complaint_contact",
                    "confidence": 1.0 if "user" in set(field_sources.values()) else 0.85,
                })
            return self._contact_result(
                cs,
                require_email=require_email,
                require_phone=require_phone,
                changed_sources=[],
                discovery_skipped=True,
            )

        original = (cs.company_email, cs.company_phone, cs.company_website)
        changed_sources: List[str] = []

        communication_text = self._complaint_contact_text(cs)
        learned = self._learn_contacts_from_text(
            cs,
            communication_text,
            source="communication_history",
            require_phone_context=True,
        )
        if learned:
            changed_sources.append("communication_history")

        shared = self._load_shared_business_contact(cs.company_name)
        if shared:
            changed = self._merge_contact_into_complaint(
                cs,
                shared,
                source="shared_business_contacts",
                confidence=float(shared.get("confidence") or 0.80),
            )
            if changed:
                changed_sources.append("shared_business_contacts")

        # A known business email gives a strong domain/website clue. This is also
        # the requested fallback for discovering a missing phone number.
        if cs.company_email and not cs.company_website:
            cs.company_website = email_to_website(cs.company_email)
            if cs.company_website:
                changed_sources.append("email_domain")
                field_sources = cs.contact_discovery.get("field_sources") or {}
                field_sources["website"] = "email_domain"
                cs.contact_discovery["field_sources"] = field_sources
                cs.contact_discovery["last_source"] = "email_domain"
                cs.contact_discovery["last_confidence"] = 0.75

        missing_email = require_email and not cs.company_email
        missing_phone = require_phone and not cs.company_phone
        if require_email or require_phone:
            lookup_needed = bool(missing_email or missing_phone)
        else:
            lookup_needed = bool(not cs.company_email or not cs.company_phone or not cs.company_website)

        cs.contact_discovery = cs.contact_discovery or {}
        last_attempt_epoch = float(cs.contact_discovery.get("last_external_attempt_epoch") or 0.0)
        ttl_seconds = max(0.0, CONTACT_LOOKUP_TTL_HOURS) * 3600.0
        external_allowed = force_external or not last_attempt_epoch or (time.time() - last_attempt_epoch >= ttl_seconds)

        if lookup_needed and external_allowed:
            cs.contact_discovery["last_external_attempt_epoch"] = time.time()
            cs.contact_discovery["last_external_attempt_at"] = now_ts()

            if cs.company_website:
                web_contact = self._crawl_website_contacts(cs.company_website, cs.company_name)
                changed = self._merge_contact_into_complaint(
                    cs,
                    web_contact,
                    source=web_contact.get("source") or "official_website",
                    source_url=web_contact.get("source_url") or cs.company_website,
                    confidence=float(web_contact.get("confidence") or 0.78),
                )
                if changed:
                    changed_sources.append("official_website")

            if (not cs.company_phone or not cs.company_website) and cs.company_name:
                places_contact = self._google_places_contact_lookup(cs)
                changed = self._merge_contact_into_complaint(
                    cs,
                    places_contact,
                    source=places_contact.get("source") or "google_places",
                    source_url=places_contact.get("source_url") or "",
                    confidence=float(places_contact.get("confidence") or 0.80),
                )
                if changed:
                    changed_sources.append("google_places")

            if (not cs.company_email or not cs.company_phone) and cs.company_name:
                custom_contact = self._custom_contact_lookup(cs)
                changed = self._merge_contact_into_complaint(
                    cs,
                    custom_contact,
                    source=custom_contact.get("source") or "contact_lookup_api",
                    source_url=custom_contact.get("source_url") or "",
                    confidence=float(custom_contact.get("confidence") or 0.65),
                )
                if changed:
                    changed_sources.append("contact_lookup_api")

            # Providers often return a website but no email. Crawl that official
            # website once more to recover mailto/tel contact details.
            if cs.company_website and (not cs.company_email or not cs.company_phone):
                web_contact = self._crawl_website_contacts(cs.company_website, cs.company_name)
                changed = self._merge_contact_into_complaint(
                    cs,
                    web_contact,
                    source=web_contact.get("source") or "official_website",
                    source_url=web_contact.get("source_url") or cs.company_website,
                    confidence=float(web_contact.get("confidence") or 0.78),
                )
                if changed:
                    changed_sources.append("official_website")

        current = (cs.company_email, cs.company_phone, cs.company_website)
        if current != original:
            detail_parts = []
            if cs.company_email:
                detail_parts.append(f"email={cs.company_email}")
            if cs.company_phone:
                detail_parts.append(f"phone={cs.company_phone}")
            if cs.company_website:
                detail_parts.append(f"website={cs.company_website}")
            cs.activities.append(asdict(ActivityEvent(
                ts=now_ts(),
                channel="system",
                kind="contact_discovery",
                title="Business contact information updated",
                detail="; ".join(detail_parts),
                meta={"sources": list(dict.fromkeys(changed_sources))},
            )))
            self._save(cs)

        if cs.company_name and any((cs.company_email, cs.company_phone, cs.company_website)):
            self._upsert_shared_business_contact(cs.company_name, {
                "email": cs.company_email,
                "phone": cs.company_phone,
                "website": cs.company_website,
                "source": (cs.contact_discovery or {}).get("last_source") or "complaint_warrior",
                "source_url": (cs.contact_discovery or {}).get("last_source_url") or "",
                "confidence": float((cs.contact_discovery or {}).get("last_confidence") or 0.70),
            })

        return self._contact_result(
            cs,
            require_email=require_email,
            require_phone=require_phone,
            changed_sources=changed_sources,
            discovery_skipped=False,
        )

    def get_business_contact_summary(self, complaint_id: str) -> Dict[str, Any]:
        cs = self.complaints[complaint_id]
        field_sources = (cs.contact_discovery or {}).get("field_sources") or {}
        return {
            "company_name": cs.company_name,
            "email": cs.company_email,
            "phone": cs.company_phone,
            "website": cs.company_website,
            "last_source": (cs.contact_discovery or {}).get("last_source") or "",
            "last_external_attempt_at": (cs.contact_discovery or {}).get("last_external_attempt_at") or "",
            "field_sources": dict(field_sources),
            "email_source": field_sources.get("email", "stored" if cs.company_email else ""),
            "phone_source": field_sources.get("phone", "stored" if cs.company_phone else ""),
            "website_source": field_sources.get("website", "stored" if cs.company_website else ""),
        }

    def update_business_contacts(
        self,
        complaint_id: str,
        *,
        email: str = "",
        phone: str = "",
        website: str = "",
    ) -> Dict[str, Any]:
        """Save user-confirmed contacts and make them authoritative for actions."""
        cs = self.complaints[complaint_id]
        email = (email or "").strip().lower()
        phone = normalize_phone(phone)
        website = (website or "").strip()
        if email and not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
            raise ValueError("Company / business email is not a valid email address.")
        if website and not re.match(r"^https?://", website, flags=re.IGNORECASE):
            website = "https://" + website.lstrip("/")

        before = (cs.company_email, cs.company_phone, cs.company_website)
        cs.company_email = email
        cs.company_phone = phone
        cs.company_website = website
        cs.contact_discovery = cs.contact_discovery or {}
        field_sources = cs.contact_discovery.get("field_sources")
        if not isinstance(field_sources, dict):
            field_sources = {}
        user_fields = set(cs.contact_discovery.get("user_provided_fields") or [])
        for field, value in (("email", email), ("phone", phone), ("website", website)):
            if value:
                field_sources[field] = "user"
                user_fields.add(field)
            else:
                field_sources.pop(field, None)
                user_fields.discard(field)
        cs.contact_discovery["field_sources"] = field_sources
        cs.contact_discovery["user_provided_fields"] = sorted(user_fields)
        cs.contact_discovery["last_source"] = "user"
        cs.contact_discovery["last_confidence"] = 1.0

        after = (cs.company_email, cs.company_phone, cs.company_website)
        if after != before:
            self._append_activity(
                cs,
                "system",
                "contact_update",
                "Business contact confirmed by user",
                f"email={email or '[blank]'}; phone={phone or '[blank]'}; website={website or '[blank]'}",
                {"source": "user", "authoritative": True},
            )
        else:
            self._save(cs)

        if cs.company_name and any(after):
            self._upsert_shared_business_contact(cs.company_name, {
                "email": email,
                "phone": phone,
                "website": website,
                "source": "user_confirmed",
                "confidence": 1.0,
            })
        return self.get_business_contact_summary(complaint_id)

    def delete_complaint(self, complaint_id: str) -> Dict[str, Any]:
        """Permanently delete one complaint owned by the active app user.

        The complaint row and complaint-specific phone-result rows are removed
        from the Complaint Warrior SQLite database. Gmail messages and local
        evidence/upload files are intentionally retained to avoid deleting
        records outside the complaint database without a separate user action.
        """
        user_email = self._require_user().strip().lower()
        complaint_id = (complaint_id or "").strip()
        if not complaint_id:
            raise ValueError("complaint_id is required")

        cs = self.complaints.get(complaint_id)
        if cs is None:
            raise KeyError(f"Complaint not found: {complaint_id}")

        owner_email = (getattr(cs, "user_email", "") or "").strip().lower()
        if owner_email and owner_email != user_email:
            raise PermissionError("This complaint belongs to another app user.")

        documents_retained = [p for p in (getattr(cs, "docs", None) or []) if p]
        evidence_pack = getattr(cs, "evidence_pack_pdf", None)
        if evidence_pack:
            documents_retained.append(evidence_pack)

        related_records_deleted: Dict[str, int] = {}
        with sqlite3.connect(self.complaint_db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute(
                "DELETE FROM complaints WHERE user_email=? AND complaint_id=?",
                (user_email, complaint_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Complaint was not deleted. It may already have been removed "
                    "or the active user does not own it."
                )

            # Remove records that are unambiguously scoped to this complaint.
            # Gmail messages and local files are deliberately outside this cleanup.
            table_names = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "call_results" in table_names:
                call_cursor = con.execute(
                    "DELETE FROM call_results WHERE call_sid LIKE ?",
                    (f"{complaint_id}:%",),
                )
                related_records_deleted["call_results"] = max(
                    0, int(call_cursor.rowcount or 0)
                )

            for table_name in ("small_claim_packets", "small_claim_efilings"):
                if table_name not in table_names:
                    continue
                columns = {
                    row[1]
                    for row in con.execute(
                        f"PRAGMA table_info('{table_name}')"
                    ).fetchall()
                }
                if "complaint_id" not in columns:
                    continue
                related_cursor = con.execute(
                    f"DELETE FROM {table_name} WHERE complaint_id=?",
                    (complaint_id,),
                )
                related_records_deleted[table_name] = max(
                    0, int(related_cursor.rowcount or 0)
                )

        del self.complaints[complaint_id]
        self._log(
            f"[manager] deleted complaint {complaint_id}; "
            f"related_records_deleted={related_records_deleted}"
        )
        return {
            "complaint_id": complaint_id,
            "phone_results_deleted": related_records_deleted.get("call_results", 0),
            "related_records_deleted": related_records_deleted,
            "documents_retained": documents_retained,
        }

    def add_complaint(self, subject: str, complaint_raw: str, user_email: str, user_name: str, auto_send_policy: str = DEFAULT_AUTO_SEND_POLICY, company_name: str = "", company_email: str = "", company_phone: str = "", company_website: str = "") -> ComplaintState:
        self._require_user()
        cid = f"CMP-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        prof = self.tp.rewrite_complaint_professional(complaint_raw, log_cb=self.log_cb)
        strat = self.tp.extract_resolution_strategy(prof, log_cb=self.log_cb)

        entered_email = (company_email or "").strip().lower()
        entered_phone = normalize_phone(company_phone)
        entered_website = (company_website or "").strip()
        resolved_email = entered_email or next(
            iter(extract_email_candidates(complaint_raw, {(user_email or "").strip().lower()})),
            "",
        )
        resolved_website = entered_website or email_to_website(resolved_email) or ""
        user_provided_fields = [
            field for field, value in (
                ("email", entered_email),
                ("phone", entered_phone),
                ("website", entered_website),
            ) if value
        ]
        field_sources = {field: "user" for field in user_provided_fields}
        if resolved_email and "email" not in field_sources:
            field_sources["email"] = "complaint_text"
        if resolved_website and "website" not in field_sources:
            field_sources["website"] = "email_domain"

        local_tid = f"LOCAL-company_support-{int(time.time()*1000)}"
        ts = ThreadState(thread_id=local_tid, label="company_support", status="open", stage="initial_demand", drafts=[], satisfaction=None)

        cs = ComplaintState(
            complaint_id=cid,
            subject=subject,
            complaint_raw=complaint_raw,
            complaint_professional=prof,
            user_email=user_email,
            user_name=user_name,
            company_name=(company_name or "").strip(),
            company_email=resolved_email,
            company_phone=entered_phone,
            company_website=resolved_website,
            contact_discovery={
                "user_provided_fields": sorted(user_provided_fields),
                "field_sources": field_sources,
                "last_source": "user" if user_provided_fields else ("complaint_text" if resolved_email else ""),
                "last_confidence": 1.0 if user_provided_fields else (0.58 if resolved_email else 0.0),
            },
            created_at=now_ts(),
            docs=[],
            evidence_pack_pdf=None,
            strategy=asdict(strat),
            current_status_summary="Initial demand ready to draft.",
            final_conclusion="",
            auto_send_policy=auto_send_policy,
            module_statuses=_empty_module_statuses(),
            threads={local_tid: ts},
            activities=[],
        )
        self.complaints[cid] = cs
        self._append_activity(cs, "system", "status", "Complaint created", "Initial demand created and ready to draft.", {"stage": "initial_demand"})
        self._save(cs)
        if cs.company_name and any((cs.company_email, cs.company_phone, cs.company_website)):
            self._upsert_shared_business_contact(cs.company_name, {
                "email": cs.company_email,
                "phone": cs.company_phone,
                "website": cs.company_website,
                "source": "user_initial_submission",
                "confidence": 0.95,
            })
        return cs

    def set_auto_send_policy(self, complaint_id: str, policy: str):
        if policy not in AUTO_SEND_POLICIES:
            raise ValueError(f"Invalid policy: {policy}")
        cs = self.complaints[complaint_id]
        cs.auto_send_policy = policy
        self._append_activity(cs, "system", "status", "Policy updated", f"Policy set to {policy}", {"policy": policy})


    def set_module_status(self, complaint_id: str, status_key: str, done: bool = True, note: str = ""):
        """Mark an external module milestone on the complaint.

        Supported status_key values:
        - resolved
        - social_network_shared
        - charge_back_initiated
        - submitted_to_small_claim_court
        - escalated_to_authorities
        """
        if status_key not in COMPLAINT_MODULE_STATUSES:
            raise ValueError(f"Invalid module status: {status_key}")

        cs = self.complaints[complaint_id]
        cs.module_statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))
        label = COMPLAINT_MODULE_STATUSES[status_key]
        cs.module_statuses[status_key] = {
            "done": bool(done),
            "label": label,
            "updated_at": now_ts(),
            "note": note or "",
        }

        if done:
            cs.current_status_summary = label
            if status_key == "resolved":
                cs.current_status_summary = "Company agreed to satisfy the demand. Complaint resolved."
                cs.final_conclusion = note or "Complaint resolved."
                # Resolved overrides all escalation recommendations. Keep historical
                # module milestones, but stop all active threads and clear pending drafts.
                for ts in cs.threads.values():
                    ts.status = "closed"
                    ts.stage = "resolved"
                    ts.drafts = []
                    ts.last_decision = None
                if cs.strategy is None:
                    cs.strategy = {}
                cs.strategy["next_recommended_resolution_status"] = {
                    "recommended_status": "resolved",
                    "title": "Complaint resolved",
                    "reason": note or "Company agreed to satisfy the customer demand.",
                    "action": "No further action is recommended. Verify that the promised remedy is actually received.",
                    "blocked": True,
                }
            elif status_key == "charge_back_initiated":
                cs.final_conclusion = "Charge-back initiated; awaiting bank decision."
            elif status_key == "submitted_to_small_claim_court":
                cs.final_conclusion = "Submitted to small claim court; awaiting hearing or settlement."
            elif status_key == "escalated_to_authorities":
                cs.final_conclusion = "Escalated to authorities; awaiting agency response."
            elif status_key == "social_network_shared":
                cs.final_conclusion = "Public social-network escalation started."

        self._append_activity(
            cs,
            "system",
            "status",
            label if done else f"{label} cleared",
            note or (f"Complaint status updated: {label}" if done else f"Complaint status cleared: {label}"),
            {"status_key": status_key, "done": bool(done)},
        )
        self._save(cs)
        return cs.module_statuses[status_key]

    def _latest_activity_ts_for_kind(self, cs: ComplaintState, *, kind: str = "received") -> str:
        latest = ""
        for ev in cs.activities or []:
            if (ev.get("kind") or "").lower() == kind.lower():
                latest = max(latest, ev.get("ts") or "")
        return latest

    def should_pause_actions(self, cs: ComplaintState) -> bool:
        """Return True when ordinary recommendations/actions should pause.

        Rules:
        - submitted_to_small_claim_court: do not recommend or perform further actions.
        - escalated_to_authorities: pause until a later inbound reply is recorded.
        """
        statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))

        if statuses.get("resolved", {}).get("done"):
            return True

        if statuses.get("submitted_to_small_claim_court", {}).get("done"):
            return True

        esc = statuses.get("escalated_to_authorities", {})
        if esc.get("done"):
            escalated_at = esc.get("updated_at") or ""
            latest_received = self._latest_activity_ts_for_kind(cs, kind="received")
            # If a reply was received after escalation, the case can be reviewed again.
            return not (latest_received and latest_received > escalated_at)

        return False

    def pause_reason(self, cs: ComplaintState) -> str:
        statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))
        if statuses.get("resolved", {}).get("done"):
            return "Complaint resolved; no further Complaint Warrior action is recommended."
        if statuses.get("submitted_to_small_claim_court", {}).get("done"):
            return "Submitted to small claim court; no further Complaint Warrior action is recommended."
        if statuses.get("escalated_to_authorities", {}).get("done") and self.should_pause_actions(cs):
            return "Escalated to authorities; waiting for replies before further action is recommended."
        return ""


    def recommend_next_resolution_status(self, complaint_id: str, thread_id: Optional[str] = None) -> Dict[str, Any]:
        """Return a rule-based recommendation for the next resolution module.

        This is intentionally conservative: it does not mark a module as complete.
        It only tells the UI which module should be considered next.
        """
        cs = self.complaints[complaint_id]
        statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))
        status_text = " ".join([
            cs.current_status_summary or "",
            cs.final_conclusion or "",
        ]).lower()

        # Resolved always has the highest priority and overrides escalation.
        if statuses.get("resolved", {}).get("done") or "resolved" in status_text or "satisfy the demand" in status_text:
            return {
                "recommended_status": "resolved",
                "title": "Complaint resolved",
                "reason": "The complaint appears resolved or the company appears to satisfy the demand.",
                "action": "No further escalation is recommended. Verify that the promised refund, repair, or compensation was actually received.",
                "blocked": True,
            }

        if statuses.get("submitted_to_small_claim_court", {}).get("done"):
            return {
                "recommended_status": "submitted_to_small_claim_court",
                "title": "Wait for court process",
                "reason": "The case has already been submitted to small claim court.",
                "action": "Do not start additional automated escalation. Track hearing, service, settlement, or judgment events.",
                "blocked": True,
            }

        if statuses.get("escalated_to_authorities", {}).get("done") and self.should_pause_actions(cs):
            return {
                "recommended_status": "escalated_to_authorities",
                "title": "Wait for authority replies",
                "reason": "The case was escalated to authorities and no later inbound reply has been logged yet.",
                "action": "Do not recommend another action until an agency/company reply is received.",
                "blocked": True,
            }

        # If a phone/email reply was just received, interpret current satisfaction state.
        ts = cs.threads.get(thread_id) if thread_id else None
        if ts is None and cs.threads:
            ts = next(iter(cs.threads.values()))
        satisfaction = getattr(ts, "satisfaction", None) or {}
        verdict = (satisfaction.get("verdict") or "").lower() if isinstance(satisfaction, dict) else ""

        if verdict == "resolved":
            return {
                "recommended_status": "resolved",
                "title": "Confirm remedy and close",
                "reason": "The latest reply was classified as satisfying the demand.",
                "action": "Confirm receipt of refund/repair/compensation, then close the complaint.",
                "blocked": True,
            }

        if verdict in {"unclear", "partial", "mixed"} or "review" in status_text or "clarifying" in status_text:
            return {
                "recommended_status": "negotiation",
                "title": "Continue negotiation / clarify remedy",
                "reason": "The latest reply does not clearly reject the demand but also does not fully resolve it.",
                "action": "Draft a clarification message asking for a concrete remedy, amount, deadline, and confirmation number.",
                "blocked": False,
            }

        # Escalation ladder after rejection/deadlock.
        rejected_or_deadlock = any(x in status_text for x in [
            "rejected", "did not accept", "denied", "refused", "deadlock", "no response", "unresolved", "failed"
        ]) or verdict == "rejected"

        if rejected_or_deadlock:
            if not statuses.get("charge_back_initiated", {}).get("done"):
                return {
                    "recommended_status": "charge_back_initiated",
                    "title": "Proceed to charge-back",
                    "reason": "The merchant/company did not satisfy the demand. A payment dispute is usually the next practical escalation if the transaction was card-paid and still eligible.",
                    "action": "Open Charge Back Initiator, prepare evidence, and mark charge-back initiated once submitted.",
                    "blocked": False,
                }
            if not statuses.get("escalated_to_authorities", {}).get("done"):
                return {
                    "recommended_status": "escalated_to_authorities",
                    "title": "Escalate to authorities",
                    "reason": "The charge-back path has been initiated or is not enough, and the company has not resolved the complaint.",
                    "action": "Submit regulatory/agency complaints and wait for replies before further actions.",
                    "blocked": False,
                }
            if not statuses.get("social_network_shared", {}).get("done"):
                return {
                    "recommended_status": "social_network_shared",
                    "title": "Share public complaint",
                    "reason": "Private and regulatory channels have not produced a clear resolution.",
                    "action": "Publish a factual, non-defamatory social-network post with evidence and desired remedy.",
                    "blocked": False,
                }
            return {
                "recommended_status": "submitted_to_small_claim_court",
                "title": "Prepare small-claims filing",
                "reason": "Negotiation and escalation steps appear exhausted.",
                "action": "Open Small Claim Court Warrior and mark submitted only after the filing is actually submitted.",
                "blocked": False,
            }

        # Default: keep negotiating.
        return {
            "recommended_status": "negotiation",
            "title": "Continue normal complaint workflow",
            "reason": "The case does not yet show a clear rejection, deadlock, or completed escalation milestone.",
            "action": "Continue drafting/sending the next message or call the company before escalating.",
            "blocked": False,
        }

    def update_after_iteration(self, complaint_id: str, thread_id: Optional[str] = None, detail: str = "") -> Dict[str, Any]:
        """Recompute overall next-step recommendation and persist it as a status activity."""
        cs = self.complaints[complaint_id]
        rec = self.recommend_next_resolution_status(complaint_id, thread_id)
        cs.strategy = cs.strategy or {}
        cs.strategy["next_recommended_resolution_status"] = rec
        self._append_activity(
            cs,
            "system",
            "decision",
            "Next resolution recommendation",
            detail or rec.get("action") or rec.get("reason") or "",
            {"thread_id": thread_id, "recommendation": rec},
        )
        self._save(cs)
        return rec

    def update_resolution_status(self, complaint_id: str, status: str, detail: str = "", meta: Optional[Dict[str, Any]] = None):
        """Compatibility wrapper used by external modules."""
        note = detail or ((meta or {}).get("detail") if isinstance(meta, dict) else "") or ""
        return self.set_module_status(complaint_id, status, done=True, note=note)

    def attach_docs(self, complaint_id: str, paths: List[str]):
        cs = self.complaints[complaint_id]
        for p in paths or []:
            if p and os.path.exists(p) and p not in cs.docs:
                cs.docs.append(p)
        self._append_activity(cs, "system", "docs", "Documents attached", f"Attached {len(paths or [])} file(s).", {"paths": paths})
        self._save(cs)

    def build_evidence_pdf(self, complaint_id: str, out_path: str):
        cs = self.complaints[complaint_id]
        build_evidence_pack_pdf(out_path, complaint_id, cs.docs)
        cs.evidence_pack_pdf = out_path
        self._append_activity(cs, "system", "docs", "Evidence pack built", os.path.basename(out_path), {"path": out_path})
        self._save(cs)

    def _combine_transcript(self, cs: ComplaintState, thread_id: str) -> str:
        parts: List[str] = []
        # email thread transcript if this is a Gmail thread
        if not thread_id.startswith("LOCAL-"):
            th = get_thread(self.service, thread_id)
            messages = sorted(th.get("messages", []) or [], key=lambda m: int(m.get("internalDate", "0")))
            for m in messages:
                payload = m.get("payload", {}) or {}
                headers = payload.get("headers", []) or []
                subj = get_header(headers, "Subject")
                frm = get_header(headers, "From")
                txt = decode_best_effort_text(payload).strip()
                parts.append(f"EMAIL From:{frm} Subject:{subj}\n{txt}")

        # phone logs for complaint
        for ev in cs.activities:
            if ev.get("channel") == "phone":
                parts.append(f"PHONE {ev.get('title')}\n{ev.get('detail')}")

        return "\n\n".join(parts).strip()

    def draft_reply_now(self, complaint_id: str, thread_id: str):
        cs = self.complaints[complaint_id]
        if self.should_pause_actions(cs):
            raise RuntimeError(self.pause_reason(cs))
        contact_status = self.ensure_business_contacts(
            complaint_id, require_email=True, require_phone=False
        )
        ts = cs.threads[thread_id]
        strategy = _resolution_strategy_from_dict(cs.strategy)
        self._ensure_gmail()
        combined = self._combine_transcript(cs, thread_id)
        user_data = {
            "available_docs": [os.path.basename(p) for p in cs.docs if os.path.exists(p)],
            "evidence_pack_pdf": os.path.basename(cs.evidence_pack_pdf) if cs.evidence_pack_pdf else None,
            "business_contact": contact_status,
        }

        decision = self.tp.decide_next(
            complaint_text=cs.complaint_professional,
            complaint_stage=ts.stage,
            current_status_summary=cs.current_status_summary,
            combined_transcript=combined,
            strategy=strategy,
            user_data=user_data,
            log_cb=self.log_cb,
        )
        ts.drafts = decision.drafts
        ts.last_decision = asdict(decision)
        ts.stage = decision.complaint_stage

        cs.current_status_summary = {
            "initial_demand": "Initial demand drafted and ready to send.",
            "awaiting_company_response": "Demand sent; waiting for company reply.",
            "negotiation": "Negotiation is underway.",
            "resolution_check": "Company appears to be offering a remedy; confirming details.",
            "resolved": "Complaint resolved.",
            "escalated": "Complaint escalated.",
        }.get(ts.stage, decision.rationale)

        self._append_activity(
            cs, "system", "decision", f"Drafted next step ({decision.action})",
            decision.rationale,
            {"thread_id": thread_id, "stage": ts.stage, "confidence": decision.confidence, "drafts": len(decision.drafts)}
        )
        self._save(cs)

    def _extract_business_email_for_draft(self, cs: ComplaintState, draft: Optional[Dict[str, str]] = None) -> str:
        """Find the real business recipient email for production sends.

        Priority:
        1) explicit/stored company_email field
        2) email in the user's complaint text

        Generated draft text is deliberately excluded because an LLM-generated
        address must never become a real send target.
        """
        sources = [
            getattr(cs, "company_email", "") or "",
            getattr(cs, "subject", "") or "",
            getattr(cs, "complaint_raw", "") or "",
            getattr(cs, "complaint_professional", "") or "",
        ]
        for source in sources:
            email = extract_first_email(source)
            if email:
                return email
        return ""

    def _resolve_outbound_recipient(self, cs: ComplaintState, draft: Dict[str, str]) -> tuple[str, Dict[str, Any]]:
        """Return recipient email and routing metadata for safe deployment."""
        intended = self._extract_business_email_for_draft(cs, draft)
        prod = is_production_deployment()
        if prod:
            if not intended:
                raise RuntimeError(
                    "Production mode requires a real business/company email. "
                    "Enter or confirm it in the Company / business email field. When that field is blank, "
                    "Complaint Warrior searches the complaint and actual communication history, shared "
                    "business contacts, a known company website or email domain, and configured contact-lookup "
                    "providers when Draft next message or Send selected drafts is used. Any discovered email is "
                    "saved into the complaint and displayed in the Company / business email field for review. "
                    "No usable email was found for this complaint; verify the company name/website or enter the email manually."
                )
            return intended, {
                "deployment_mode": "prod",
                "recipient_source": "company_email_or_complaint_text",
                "intended_recipient": intended,
                "actual_recipient": intended,
                "test_redirect": False,
            }

        return TEST_INBOX_EMAIL, {
            "deployment_mode": "debug",
            "recipient_source": "debug_test_inbox",
            "intended_recipient": intended,
            "actual_recipient": TEST_INBOX_EMAIL,
            "test_redirect": True,
        }



    def get_outbound_recipient_preview(self, complaint_id: str, thread_id: str, draft_index: int = 0) -> Dict[str, Any]:
        """Return visible recipient routing info before sending.

        In prod this returns the real company/business email.
        In debug/dev this returns TEST_INBOX_EMAIL as actual recipient and the
        business email, if found, as intended recipient.
        """
        cs = self.complaints[complaint_id]
        ts = cs.threads[thread_id]
        drafts = ts.drafts or []
        draft = drafts[draft_index] if 0 <= draft_index < len(drafts) else {}
        try:
            _recipient, meta = self._resolve_outbound_recipient(cs, draft)
            return meta
        except Exception as e:
            return {
                "deployment_mode": "prod" if is_production_deployment() else "debug",
                "recipient_source": "error",
                "intended_recipient": self._extract_business_email_for_draft(cs, draft),
                "actual_recipient": "",
                "test_redirect": not is_production_deployment(),
                "error": str(e),
            }

    def send_selected_drafts(self, complaint_id: str, thread_id: str, draft_indexes: List[int], attachments: Optional[List[str]] = None) -> List[dict]:
        cs = self.complaints[complaint_id]
        if self.should_pause_actions(cs):
            raise RuntimeError("Cannot send drafts while case is paused. " + self.pause_reason(cs))
        self.ensure_business_contacts(complaint_id, require_email=True, require_phone=False)
        ts = cs.threads[thread_id]
        attachments = attachments or []
        sent = []
        for i in draft_indexes:
            if i < 0 or i >= len(ts.drafts or []):
                continue
            d = ts.drafts[i]
            agent = d.get("to_hint") or ts.label or "agent"
            subject = f"[AGENT={agent}] {d.get('subject') or cs.subject}"
            body = d.get("body") or ""
            to_email, routing_meta = self._resolve_outbound_recipient(cs, d)
            intended_contact = routing_meta.get("intended_recipient") or ""
            if intended_contact and not cs.company_email:
                cs.company_email = intended_contact.strip().lower()
                if not cs.company_website:
                    cs.company_website = email_to_website(cs.company_email)
                self._upsert_shared_business_contact(cs.company_name, {
                    "email": cs.company_email, "phone": cs.company_phone,
                    "website": cs.company_website, "source": "outbound_email",
                    "confidence": 0.90,
                })
            if routing_meta.get("test_redirect"):
                body = (
                    "DEBUG / NON-PRODUCTION SEND\n"
                    f"Intended business recipient: {routing_meta.get('intended_recipient') or '[not found]'}\n"
                    f"Actual recipient: {routing_meta.get('actual_recipient')}\n\n"
                    + body
                )

            self._ensure_gmail()
            res = send_email_with_attachments(
                self.service,
                to_email=to_email,
                subject=subject,
                body_text=body,
                attachments=[p for p in attachments if p and os.path.exists(p)],
                thread_id=None if thread_id.startswith("LOCAL-") else thread_id,
            )
            sent.append(res)

            # if local thread got sent, replace local id with actual gmail thread id if present
            if thread_id.startswith("LOCAL-") and res.get("threadId"):
                new_tid = res["threadId"]
                cs.threads[new_tid] = ThreadState(
                    thread_id=new_tid,
                    label=ts.label,
                    status=ts.status,
                    stage="awaiting_company_response",
                    last_handled_msg_id=None,
                    last_decision=ts.last_decision,
                    drafts=ts.drafts,
                    satisfaction=ts.satisfaction,
                )
                del cs.threads[thread_id]
                thread_id = new_tid
                ts = cs.threads[new_tid]

            self._append_activity(
                cs, "email", "sent", "Email sent",
                body[:500],
                {"thread_id": thread_id, "gmail_msg_id": res.get("id"), "gmail_thread_id": res.get("threadId"), "subject": subject, **routing_meta}
            )

        cs.current_status_summary = "Initial demand sent; waiting for company response." if ts.stage == "awaiting_company_response" else cs.current_status_summary
        self._save(cs)
        return sent

    def _newest_unprocessed_inbound(self, gmail_thread_id: str, last_handled_msg_id: Optional[str]) -> Optional[dict]:
        th = get_thread(self.service, gmail_thread_id)
        messages = sorted(th.get("messages", []) or [], key=lambda m: int(m.get("internalDate", "0")))
        for m in reversed(messages):
            if last_handled_msg_id and m.get("id") == last_handled_msg_id:
                return None
            lbls = set(m.get("labelIds") or [])
            if self.processed_label_id in lbls or "SENT" in lbls:
                continue
            return m
        return None

    def _apply_satisfaction(self, cs: ComplaintState, ts: ThreadState, inbound_text: str, trusted: bool):
        # Build strategy safely
        strategy = _resolution_strategy_from_dict(cs.strategy)

        det = self.tp.detect_satisfaction_with_fallback(
            inbound_text=inbound_text,
            strategy=strategy,
            trusted=trusted,
            log_cb=self.log_cb,
        )

        ts.satisfaction = asdict(det)

        # log satisfaction decision into activity timeline
        self._append_activity(
            cs,
            "system",
            "satisfaction",
            "Satisfaction decision",
            det.reason,
            {
                "thread_id": ts.thread_id,
                "verdict": det.verdict,
                "gpt_used": getattr(det, "gpt_used", False),
                "signals": det.signals,
            },
        )

        if det.verdict == "resolved":
            ts.status = "resolved"
            ts.stage = "resolved"
            ts.drafts = []  # IMPORTANT: clear drafts so loop stops
            ts.last_decision = None

            cs.current_status_summary = "Company agreed to satisfy the demand. Complaint resolved."
            cs.final_conclusion = det.reason
            cs.module_statuses = _normalize_module_statuses(getattr(cs, "module_statuses", None))
            cs.module_statuses["resolved"] = {
                "done": True,
                "label": COMPLAINT_MODULE_STATUSES["resolved"],
                "updated_at": now_ts(),
                "note": det.reason or "Company agreed to satisfy the customer demand.",
            }
            cs.strategy = cs.strategy or {}
            cs.strategy["next_recommended_resolution_status"] = {
                "recommended_status": "resolved",
                "title": "Complaint resolved",
                "reason": det.reason or "Company agreed to satisfy the customer demand.",
                "action": "No further action is recommended. Verify that the promised remedy is actually received.",
                "blocked": True,
            }

            self._append_activity(
                cs,
                "system",
                "status",
                "Complaint resolved",
                det.reason,
                {
                    "thread_id": ts.thread_id,
                    "gpt_used": getattr(det, "gpt_used", False),
                    "signals": det.signals,
                },
            )

            self._save(cs)
            return True

        elif det.verdict == "rejected":
            ts.status = "open"
            ts.stage = "negotiation"
            cs.current_status_summary = "Company rejected or did not accept the demand. Negotiation continues."
            self._save(cs)
            return False

        else:
            ts.status = "open"
            ts.stage = "resolution_check"
            cs.current_status_summary = "Company response is under review; clarifying whether the remedy is acceptable."
            self._save(cs)
            return False

    def poll_once(self, trusted: bool = False):
        self._require_user()
        for cid, cs in list(self.complaints.items()):
            statuses_pause = _normalize_module_statuses(getattr(cs, "module_statuses", None))
            if statuses_pause.get("submitted_to_small_claim_court", {}).get("done"):
                continue
            for tid, ts in list(cs.threads.items()):
                if tid.startswith("LOCAL-") or ts.status == "resolved":
                    continue
                self._ensure_gmail()
                msg = self._newest_unprocessed_inbound(tid, ts.last_handled_msg_id)
                if not msg:
                    continue

                payload = msg.get("payload", {}) or {}
                headers = payload.get("headers", []) or []
                frm = get_header(headers, "From")
                subj = get_header(headers, "Subject")
                inbound_text = decode_best_effort_text(payload).strip()
                discovered_fields = self._learn_contacts_from_text(
                    cs,
                    f"From: {frm}\n{inbound_text}",
                    source="communication_inbound_email",
                    require_phone_context=False,
                )
                if discovered_fields and cs.company_name:
                    self._upsert_shared_business_contact(cs.company_name, {
                        "email": cs.company_email, "phone": cs.company_phone,
                        "website": cs.company_website,
                        "source": "communication_inbound_email",
                        "confidence": 0.90,
                    })

                self._append_activity(cs, "email", "received", f"Email received from {frm}", inbound_text[:1000], {"thread_id": tid, "subject": subj, "msg_id": msg.get("id")})
                ts.last_handled_msg_id = msg.get("id")
                add_label(self.service, msg["id"], self.processed_label_id)

                resolved = self._apply_satisfaction(cs, ts, inbound_text=inbound_text, trusted=trusted)

                # If resolved, stop the loop completely
                if resolved:
                    self._save(cs)
                    return

                # Otherwise continue workflow unless the case remains paused.
                if self.should_pause_actions(cs):
                    self._save(cs)
                    continue

                self.draft_reply_now(cid, tid)

                # Manual mode still drafts but does NOT auto-send
                if trusted and cs.auto_send_policy == "auto_send":
                    self.send_selected_drafts(cid, tid, [0], attachments=cs.docs)

                self._save(cs)


    # -------------------- no-email-reply phone follow-up --------------------
    def _parse_activity_ts(self, value: str) -> Optional[datetime]:
        """Parse stored activity timestamps safely."""
        if not value:
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except Exception:
            # Older activities may use "YYYY-mm-dd HH:MM:SS".
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(str(value).strip(), fmt)
                except Exception:
                    pass
        return None

    def _latest_thread_activity(self, cs: ComplaintState, thread_id: str, *, channel: Optional[str] = None, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the latest activity matching a thread/channel/kind filter."""
        best = None
        best_dt = None
        for ev in cs.activities or []:
            meta = ev.get("meta") or {}
            if thread_id and meta.get("thread_id") != thread_id:
                continue
            if channel and (ev.get("channel") or "").lower() != channel.lower():
                continue
            if kind and (ev.get("kind") or "").lower() != kind.lower():
                continue
            ts_dt = self._parse_activity_ts(ev.get("ts") or "")
            if ts_dt is None:
                continue
            if best_dt is None or ts_dt > best_dt:
                best = ev
                best_dt = ts_dt
        return best

    def get_phone_followup_status(self, complaint_id: str, thread_id: str, days_without_reply: int = 3, max_attempts: int = 2) -> Dict[str, Any]:
        """Return whether phone follow-up is due after no email reply.

        Rule:
        - After an email is sent on this thread, wait days_without_reply days.
        - If no later inbound email/phone reply is logged, phone follow-up is due.
        - Automated mode may place up to max_attempts phone calls for that sent email.
        """
        cs = self.complaints[complaint_id]
        sent_ev = self._latest_thread_activity(cs, thread_id, channel="email", kind="sent")
        if not sent_ev:
            return {
                "due": False,
                "reason": "No sent email found for this thread.",
                "days_without_reply": 0,
                "attempts": 0,
                "remaining_attempts": max_attempts,
                "last_sent_ts": "",
            }

        sent_ts = sent_ev.get("ts") or ""
        sent_dt = self._parse_activity_ts(sent_ts)
        if not sent_dt:
            return {
                "due": False,
                "reason": "Could not parse last sent email timestamp.",
                "days_without_reply": 0,
                "attempts": 0,
                "remaining_attempts": max_attempts,
                "last_sent_ts": sent_ts,
            }

        now_dt = datetime.now(sent_dt.tzinfo) if sent_dt.tzinfo else datetime.now()
        age_days = max(0.0, (now_dt - sent_dt).total_seconds() / 86400.0)

        # Any later inbound reply (email or phone transcript) clears the no-reply condition.
        latest_inbound = None
        latest_inbound_dt = None
        for ev in cs.activities or []:
            meta = ev.get("meta") or {}
            if meta.get("thread_id") != thread_id:
                continue
            if (ev.get("kind") or "").lower() != "received":
                continue
            if (ev.get("channel") or "").lower() not in {"email", "phone"}:
                continue
            ts_dt = self._parse_activity_ts(ev.get("ts") or "")
            if ts_dt and ts_dt > sent_dt and (latest_inbound_dt is None or ts_dt > latest_inbound_dt):
                latest_inbound = ev
                latest_inbound_dt = ts_dt

        if latest_inbound_dt:
            return {
                "due": False,
                "reason": "A reply was received after the last sent email.",
                "days_without_reply": age_days,
                "attempts": 0,
                "remaining_attempts": max_attempts,
                "last_sent_ts": sent_ts,
                "latest_reply_ts": latest_inbound.get("ts") or "",
            }

        attempts = 0
        for ev in cs.activities or []:
            meta = ev.get("meta") or {}
            if meta.get("thread_id") != thread_id:
                continue
            if not meta.get("auto_followup_no_email_reply"):
                continue
            if meta.get("email_sent_ts") == sent_ts:
                attempts += 1

        due = age_days >= float(days_without_reply) and attempts < max_attempts and not self.should_pause_actions(cs)
        remaining = max(0, max_attempts - attempts)
        if due:
            reason = f"No email reply for {age_days:.1f} days after the last sent email; phone follow-up is due."
        elif attempts >= max_attempts:
            reason = "Maximum automatic phone follow-up attempts reached for the last sent email."
        elif self.should_pause_actions(cs):
            reason = self.pause_reason(cs)
        else:
            reason = f"Waiting for email reply; phone follow-up becomes due after {days_without_reply} days."
        return {
            "due": bool(due),
            "reason": reason,
            "days_without_reply": age_days,
            "attempts": attempts,
            "remaining_attempts": remaining,
            "max_attempts": max_attempts,
            "last_sent_ts": sent_ts,
            "threshold_days": days_without_reply,
        }

    def automated_phone_followup_if_due(self, complaint_id: str, thread_id: str, trusted: bool = False, days_without_reply: int = 3, max_attempts: int = 2) -> List[str]:
        """Place up to max_attempts phone calls when no email reply arrived after 3 days."""
        replies: List[str] = []
        while True:
            status = self.get_phone_followup_status(complaint_id, thread_id, days_without_reply, max_attempts)
            if not status.get("due"):
                break
            cs = self.complaints[complaint_id]
            attempt_no = int(status.get("attempts") or 0) + 1
            self._append_activity(
                cs,
                "system",
                "decision",
                "Automatic phone follow-up due",
                status.get("reason") or "No email reply after threshold; placing phone call.",
                {
                    "thread_id": thread_id,
                    "auto_followup_no_email_reply": True,
                    "attempt_no": attempt_no,
                    "email_sent_ts": status.get("last_sent_ts") or "",
                    "threshold_days": days_without_reply,
                },
            )
            try:
                reply = self.place_phone_call_and_capture_reply(complaint_id, thread_id)
                replies.append(reply)
                # Mark this attempt after the call so it is counted even if no transcript is captured.
                self._append_activity(
                    self.complaints[complaint_id],
                    "phone",
                    "call",
                    f"Automatic phone follow-up attempt {attempt_no}",
                    "Auto-call completed after no email reply for 3 days." + (" Transcript captured." if reply else " No transcript captured."),
                    {
                        "thread_id": thread_id,
                        "auto_followup_no_email_reply": True,
                        "attempt_no": attempt_no,
                        "email_sent_ts": status.get("last_sent_ts") or "",
                        "transcript_empty": not bool(reply),
                    },
                )
                if reply and reply.strip():
                    break
            except Exception as exc:
                self._append_activity(
                    self.complaints[complaint_id],
                    "phone",
                    "call",
                    f"Automatic phone follow-up attempt {attempt_no} failed",
                    str(exc),
                    {
                        "thread_id": thread_id,
                        "auto_followup_no_email_reply": True,
                        "attempt_no": attempt_no,
                        "email_sent_ts": status.get("last_sent_ts") or "",
                        "error": True,
                    },
                )
                break
        return replies

    def automated_step(self, trusted: bool = False):
        """
        Logical sequencing:
        1) draft/send initial demand if complaint is new and no email has been sent
        2) poll for company reply
        3) if unresolved, draft negotiation follow-up
        4) if satisfied, close complaint visibly
        """
        self._require_user()
        for cid, cs in list(self.complaints.items()):
            if self.should_pause_actions(cs):
                self._log(f"[manager] paused {cid}: {self.pause_reason(cs)}")
                continue
            # seed any empty complaint with a first draft
            for tid, ts in list(cs.threads.items()):
                if not ts.drafts and ts.status != "resolved":
                    self.draft_reply_now(cid, tid)
                    if trusted and cs.auto_send_policy == "auto_send" and ts.stage == "awaiting_company_response":
                        self.send_selected_drafts(cid, tid, [0], attachments=cs.docs)
        self._ensure_gmail()
        self.poll_once(trusted=trusted)

        # After polling email, if a sent email has had no reply for 3 days,
        # automated mode performs up to two phone follow-up attempts.
        for cid, cs in list(self.complaints.items()):
            if self.should_pause_actions(cs):
                continue
            for tid, ts in list(cs.threads.items()):
                if tid.startswith("LOCAL-") or ts.status == "resolved":
                    continue
                self.automated_phone_followup_if_due(cid, tid, trusted=trusted, days_without_reply=3, max_attempts=2)

    def place_phone_call_and_capture_reply(self, complaint_id: str, thread_id: str, timeout: int = 300) -> str:
        cs = self.complaints[complaint_id]
        if self.should_pause_actions(cs):
            raise RuntimeError("Cannot place phone call while case is paused. " + self.pause_reason(cs))
        contact_status = self.ensure_business_contacts(
            complaint_id, require_email=False, require_phone=True
        )
        target_phone = normalize_phone(contact_status.get("phone") or "")
        if not target_phone:
            raise RuntimeError(
                "No business phone number is available. Enter it in Company phone, or provide a company "
                "email/website so Complaint Warrior can try to discover the phone before calling."
            )
        ts = cs.threads[thread_id]
        if ComplaintCallAgent is None:
            raise RuntimeError("Phone module is not available (ComplaintCallAgent import failed).")

        agent = ComplaintCallAgent("config.ini")
        call_kwargs = {
            "user_complaint": cs.complaint_raw or cs.complaint_professional,
            "vendor_hint": cs.company_name or (None if ts.label in ("company_support", "agent") else ts.label),
            "timeout": timeout,
            "complaint_stage": ts.stage,
            "current_status_summary": cs.current_status_summary,
        }
        try:
            parameters = inspect.signature(agent.call_and_get_reply_autoroute).parameters
        except Exception:
            parameters = {}
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
        phone_argument = next(
            (name for name in ("vendor_phone", "phone_number", "business_phone", "target_phone") if name in parameters),
            "vendor_phone" if accepts_kwargs else "",
        )
        if not phone_argument:
            raise RuntimeError(
                "The phone agent cannot accept a direct target number, so Complaint Warrior refused to auto-route "
                f"the call and risk calling the wrong business. Update ComplaintCallAgent.call_and_get_reply_autoroute "
                f"to accept vendor_phone (selected number: {target_phone})."
            )
        call_kwargs[phone_argument] = target_phone
        for names, value in (
            (("vendor_email", "business_email", "contact_email"), contact_status.get("email")),
            (("vendor_website", "business_website"), contact_status.get("website")),
        ):
            if not value:
                continue
            name = next((candidate for candidate in names if candidate in parameters), names[0] if accepts_kwargs else "")
            if name:
                call_kwargs[name] = value

        phone_source = contact_status.get("phone_source") or "stored"
        self._append_activity(
            cs,
            "phone",
            "call",
            "Phone call initiated",
            f"Calling {cs.company_name or 'the company'} at {target_phone}.",
            {
                "thread_id": thread_id,
                "target_phone": target_phone,
                "phone_source": phone_source,
                "phone_argument": phone_argument,
                "contact_discovery_skipped": bool(contact_status.get("discovery_skipped")),
            },
        )
        self._log(f"[phone] calling {cs.company_name or 'company'} at {target_phone} (source={phone_source})")
        reply = agent.call_and_get_reply_autoroute(**call_kwargs)
        reply = (reply or "").strip()
        if reply:
            discovered_fields = self._learn_contacts_from_text(
                cs, reply, source="communication_phone_transcript", require_phone_context=False
            )
            if discovered_fields and cs.company_name:
                self._upsert_shared_business_contact(cs.company_name, {
                    "email": cs.company_email, "phone": cs.company_phone,
                    "website": cs.company_website,
                    "source": "communication_phone_transcript",
                    "confidence": 0.78,
                })

        self.call_store.set(
            f"{complaint_id}:{thread_id}:{int(time.time())}",
            {
                "transcript": reply,
                "thread_id": thread_id,
                "complaint_id": complaint_id,
                "target_phone": target_phone,
                "phone_source": phone_source,
                "created_at": now_ts(),
            },
        )

        if not reply:
            cs.current_status_summary = "Phone call completed, but no agent reply/transcript was captured."
            self._append_activity(
                cs,
                "phone",
                "received",
                "Phone call completed without captured reply",
                "The call was placed, but no inbound agent transcript was captured. Review Twilio logs or try again.",
                {"thread_id": thread_id, "transcript_empty": True, "target_phone": target_phone, "phone_source": phone_source},
            )
            self.update_after_iteration(
                complaint_id,
                thread_id,
                detail="Phone call produced no captured reply, so no escalation status was changed.",
            )
            self._save(cs)
            return ""

        # A phone transcript is an inbound reply. This also unpauses cases that were
        # escalated_to_authorities and were waiting for a later reply.
        self._append_activity(
            cs,
            "phone",
            "received",
            "Phone call reply",
            reply[:2000],
            {"thread_id": thread_id, "transcript_empty": False, "target_phone": target_phone, "phone_source": phone_source},
        )

        resolved = self._apply_satisfaction(
            cs,
            ts,
            inbound_text=reply,
            trusted=(cs.auto_send_policy == "auto_send"),
        )

        if resolved:
            self.update_after_iteration(complaint_id, thread_id, detail="Phone reply resolved the complaint.")
            self._save(cs)
            return reply

        rec = self.update_after_iteration(complaint_id, thread_id, detail="Phone reply processed; next resolution status recomputed.")

        # Do not draft if the recomputed status says the case is paused/blocked.
        if rec.get("blocked") or self.should_pause_actions(cs):
            self._save(cs)
            return reply

        try:
            self.draft_reply_now(complaint_id, thread_id)
        except Exception as e:
            self._append_activity(
                cs,
                "system",
                "decision",
                "Phone reply received, but follow-up draft failed",
                str(e),
                {"thread_id": thread_id},
            )

        self._save(cs)
        return reply

