# complaint_manager_phone.py
# -*- coding: utf-8 -*-
#ssh -i C:\Users\User\.ssh\aws_ec2_key.pem ec2-user@54.82.56.2

import os
import re
import json
import time
import base64
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import mimetypes

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from storage import ComplaintStore, CallResultStore
from text_processor import TextProcessing, AgentDecision

from call import ComplaintCallAgent

# -------------------- Gmail / App constants --------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

TOKEN_JSON = "token.json"
CREDENTIALS_JSON = "credentials.json"
STATE_FILE = "complaint_state.json"  # legacy (single-user JSON). Multi-user uses SQLite.
LABEL_PROCESSED = "CW_PROCESSED"

DEFAULT_POLL_SECONDS = 45

# Harness behavior: ALWAYS send drafts here
TEST_INBOX_EMAIL = "bgalitsky@hotmail.com"

AUTO_SEND_POLICIES = ("off", "spawned_only", "all")
DEFAULT_AUTO_SEND_POLICY = "off"


# -------------------- Utilities --------------------

def now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n", html)
    html = re.sub(r"(?is)<.*?>", " ", html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s+\n", "\n\n", html)
    return html.strip()


def decode_best_effort_text(payload) -> str:
    """
    Robust text extraction: text/plain -> text/html -> any text/* -> recurse nested multiparts.
    """
    if not payload:
        return ""

    def decode_data(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    body = payload.get("body", {}) or {}
    if body.get("data"):
        mt = payload.get("mimeType", "")
        txt = decode_data(body["data"])
        return strip_html(txt) if mt == "text/html" else txt

    parts = payload.get("parts", []) or []

    def find_part(mime: str) -> Optional[str]:
        for p in parts:
            if p.get("mimeType") == mime:
                pdata = (p.get("body") or {}).get("data")
                if pdata:
                    return decode_data(pdata)
        return None

    txt = find_part("text/plain")
    if txt:
        return txt.strip()

    html = find_part("text/html")
    if html:
        return strip_html(html)

    for p in parts:
        if (p.get("mimeType") or "").startswith("text/"):
            pdata = (p.get("body") or {}).get("data")
            if pdata:
                return strip_html(decode_data(pdata))

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


def get_gmail_creds_local():
    creds = None
    if os.path.exists(TOKEN_JSON):
        creds = Credentials.from_authorized_user_file(TOKEN_JSON, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_JSON):
                raise FileNotFoundError(
                    "Missing credentials.json next to scripts (Google Cloud OAuth client)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_JSON, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_JSON, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


#def build_gmail_service():
#    creds = get_gmail_creds()
#    return build("gmail", "v1", credentials=creds)

from typing import Optional
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# IMPORTANT: adjust import path if needed based on your package name
# Your tree shows gmail_token_store.py in the same folder as complaint_manager_phone.py
from gmail_token_store import GmailTokenStore


def build_gmail_service(
    token_db_path: str = "cw_gmail_tokens.sqlite",
    token_key: str = "default",
    user_agent: str = "ComplaintWarrior/1.0",
):
    """
    Headless Gmail API client builder for EC2.

    Reads OAuth token JSON from SQLite via GmailTokenStore:
      token_json = {
        "token": "...",
        "refresh_token": "...",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "...",
        "client_secret": "...",
        "scopes": [...]
      }

    If access token is expired, refreshes using refresh_token and writes
    updated token back to SQLite. No token.json usage.
    """
    store = GmailTokenStore(db_path=token_db_path)
    token_json = store.get(token_key)

    if not token_json:
        raise RuntimeError(
            "Gmail is not connected. No token found in SQLite. "
            "Open the Streamlit UI and click 'Connect Gmail' first."
        )

    creds = Credentials(
        token=token_json.get("token"),
        refresh_token=token_json.get("refresh_token"),
        token_uri=token_json.get("token_uri"),
        client_id=token_json.get("client_id"),
        client_secret=token_json.get("client_secret"),
        scopes=token_json.get("scopes"),
    )

    # Refresh if needed (headless)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # persist updated access token back to SQLite
            token_json["token"] = creds.token
            # refresh_token may be None on refresh; keep existing if missing
            if getattr(creds, "refresh_token", None):
                token_json["refresh_token"] = creds.refresh_token
            store.set(token_key, token_json)
        else:
            raise RuntimeError(
                "Gmail credentials are invalid and cannot be refreshed. "
                "Re-connect Gmail via OAuth."
            )

    # cache_discovery=False avoids filesystem writes and is safer in containers
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service


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


def send_email_with_attachments(
    service,
    to_email: str,
    subject: str,
    body_text: str,
    attachments: Optional[List[str]] = None,
    thread_id: Optional[str] = None,
):
    attachments = attachments or []
    msg = MIMEMultipart()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    for path in attachments:
        if not os.path.exists(path):
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

    if not files:
        line("  (none)")
    else:
        for p in files:
            if os.path.exists(p):
                size = os.path.getsize(p)
                line(f"  - {os.path.basename(p)} ({size} bytes)")
            else:
                line(f"  - MISSING: {p}")

    c.save()


# -------------------- State models --------------------

@dataclass
class ThreadState:
    thread_id: str
    label: str  # agent label: airline_support / regulator / mediator ...
    status: str = "open"  # open|waiting|resolved|abandoned|escalated
    parent_thread_id: Optional[str] = None

    last_handled_msg_id: Optional[str] = None
    last_inbound: Optional[Dict[str, Any]] = None  # cached view of latest inbound
    timeline: List[Dict[str, str]] = None

    # Cross-channel state (email -> phone)
    last_outbound_sent_at: Optional[str] = None
    last_phone_reply: Optional[Dict[str, Any]] = None  # {ts, vendor, to, text, meta}

    last_decision: Optional[Dict[str, Any]] = None
    last_draft: Optional[Dict[str, str]] = None
    drafts: List[Dict[str, str]] = None  # store all drafts from last decision

    def add_event(self, kind: str, detail: str):
        self.timeline = self.timeline or []
        self.timeline.append({"ts": now_ts(), "kind": kind, "detail": detail})


@dataclass
class ComplaintState:
    complaint_id: str
    subject: str
    complaint_raw: str
    complaint_professional: str
    created_at: str
    docs: List[str]
    evidence_pack_pdf: Optional[str]
    threads: Dict[str, ThreadState]

    safe_mode: bool = True
    user_email: str = ""
    user_name: str = ""
    auto_send_policy: str = DEFAULT_AUTO_SEND_POLICY  # off | spawned_only | all

    def to_json(self) -> dict:
        d = asdict(self)
        d["threads"] = {tid: asdict(ts) for tid, ts in self.threads.items()}
        return d

    @staticmethod
    def from_json(d: dict) -> "ComplaintState":
        threads = {}
        for tid, t in (d.get("threads") or {}).items():

            ts = ThreadState(
                thread_id=t["thread_id"],
                label=t.get("label", "unknown"),
                status=t.get("status", "open"),
                parent_thread_id=t.get("parent_thread_id"),
                last_handled_msg_id=t.get("last_handled_msg_id"),
                last_inbound=t.get("last_inbound"),
                timeline=t.get("timeline", []) or [],
                last_outbound_sent_at=t.get("last_outbound_sent_at"),
                last_phone_reply=t.get("last_phone_reply"),
                last_decision=t.get("last_decision"),
                last_draft=t.get("last_draft"),
                drafts=t.get("drafts", []) or [],
            )
            threads[tid] = ts

        pol = d.get("auto_send_policy", DEFAULT_AUTO_SEND_POLICY)
        if pol not in AUTO_SEND_POLICIES:
            pol = DEFAULT_AUTO_SEND_POLICY

        return ComplaintState(
            complaint_id=d["complaint_id"],
            subject=d.get("subject", ""),
            complaint_raw=d.get("complaint_raw", ""),
            complaint_professional=d.get("complaint_professional", ""),
            created_at=d.get("created_at", now_ts()),
            docs=d.get("docs", []) or [],
            evidence_pack_pdf=d.get("evidence_pack_pdf"),
            threads=threads,
            safe_mode=bool(d.get("safe_mode", True)),
            user_email=d.get("user_email", ""),
            user_name=d.get("user_name", ""),
            auto_send_policy=pol,
        )


# -------------------- Manager --------------------

class ComplaintWarriorManager:
    """
    Orchestrates multiple threads per complaint.

    Key behavior:
    - Poll Gmail threads for inbound (non-SENT) messages
    - On inbound -> GPT decides next -> store drafts
    - If GPT action is spawn_mediator_agent or escalate:
        create new agent threads automatically (one per unique to_hint label)
    - Auto-send policy per complaint:
        off:          never auto-send
        spawned_only: auto-send only for spawned threads (parent_thread_id != None)
        all:          auto-send draft[0] for any thread with new drafts
    - SEND button behavior (used by UI):
        send draft "to agent X" => subject contains [AGENT=X], send to TEST_INBOX_EMAIL
    """

    def __init__(
        self,
        text_processor: TextProcessing,
        state_file: str = STATE_FILE,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        log_cb: Optional[Callable[[str], None]] = None,
        *,
        user_email: Optional[str] = None,
        gmail_user_key: Optional[str] = None,
        token_db_path: str = "cw_gmail_tokens.sqlite",
        store_db_path: str = "cw_store.sqlite",
    ):
        self.tp = text_processor
        self.state_file = state_file
        self.poll_seconds = poll_seconds
        self.log_cb = log_cb or (lambda s: None)

        # Multi-user persistence (complaints + call results)
        self.store_db_path = store_db_path
        self.store = ComplaintStore(db_path=self.store_db_path)
        self.call_store = CallResultStore(db_path=self.store_db_path)

        # Active app user (complaints are scoped by this email)
        self.active_user_email: Optional[str] = None
        if user_email:
            self.set_user(user_email)

        # Gmail identity (token key). Default to user_email.
        self.gmail_user_key = gmail_user_key or self.active_user_email or "default"
        self.token_db_path = token_db_path

        # Gmail may not be connected yet (first-time user). Allow headless init.
        self.service = None
        self.processed_label_id = None
        try:
            self.service = build_gmail_service(token_db_path=self.token_db_path, token_key=self.gmail_user_key)
            self.processed_label_id = ensure_label(self.service, LABEL_PROCESSED)
        except Exception as e:
            self.log_cb(f"[gmail] Not connected yet for key={self.gmail_user_key}: {e}")

        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self.complaints: Dict[str, ComplaintState] = self._load_state()

    # ---------- user / gmail switching (multi-user) ----------
    def set_user(self, user_email: str):
        """Select the active app user (complaints are scoped by this email)."""
        if not user_email:
            raise ValueError("user_email is required")
        with self._lock:
            self.active_user_email = user_email.strip().lower()
            # If caller didn't explicitly pick a gmail_user_key, keep them aligned.
            if getattr(self, "gmail_user_key", None) in (None, "default"):
                self.gmail_user_key = self.active_user_email
            self.complaints = self._load_state()
            self.log_cb(f"[user] Active user: {self.active_user_email}")

    def set_gmail_user(self, gmail_user_key: str):
        """Switch Gmail token identity (key in cw_gmail_tokens.sqlite)."""
        if not gmail_user_key:
            raise ValueError("gmail_user_key is required")
        with self._lock:
            self.gmail_user_key = gmail_user_key
            self.service = build_gmail_service(token_db_path=self.token_db_path, token_key=self.gmail_user_key)
            self.processed_label_id = ensure_label(self.service, LABEL_PROCESSED)
            self.log_cb(f"[gmail] Active Gmail key: {self.gmail_user_key}")

    def _gmail(self):
        """Return connected Gmail service or raise a clear error."""
        if not self.service or not self.processed_label_id:
            raise RuntimeError(
                "Gmail is not connected for this user. "
                "Use the UI 'Connect Gmail' flow and then select the same email as the active user."
            )
        return self.service

    # ---------- persistence ----------
    def _load_state(self) -> Dict[str, ComplaintState]:
        # Multi-user: load from SQLite when active_user_email is set.
        if self.active_user_email:
            raw_map = self.store.load_all(self.active_user_email)
            out: Dict[str, ComplaintState] = {}
            for cid, d in raw_map.items():
                out[cid] = ComplaintState.from_json(d)
            return out

        # Fallback legacy single-user JSON file.
        if not os.path.exists(self.state_file):
            return {}
        with open(self.state_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out: Dict[str, ComplaintState] = {}
        for cid, d in raw.items():
            out[cid] = ComplaintState.from_json(d)
        return out

    def _save_state(self):
        with self._lock:
            if self.active_user_email:
                for cid, cs in self.complaints.items():
                    self.store.upsert(self.active_user_email, cid, cs.to_json())
                return

            # Legacy fallback
            raw = {cid: cs.to_json() for cid, cs in self.complaints.items()}
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)

    # Public helper if you want UI to update policy cleanly
    def set_auto_send_policy(self, complaint_id: str, policy: str):
        if policy not in AUTO_SEND_POLICIES:
            raise ValueError(f"Invalid policy: {policy}")
        with self._lock:
            cs = self.complaints[complaint_id]
            cs.auto_send_policy = policy
            self._save_state()

    # ---------- complaint CRUD ----------
    def add_complaint(
        self,
        subject: str,
        complaint_raw: str,
        user_email: str,
        user_name: str,
        safe_mode: bool = True,
        auto_send_policy: str = DEFAULT_AUTO_SEND_POLICY,
    ) -> ComplaintState:
        if auto_send_policy not in AUTO_SEND_POLICIES:
            auto_send_policy = DEFAULT_AUTO_SEND_POLICY

        with self._lock:
            cid = f"CMP-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            prof = self.tp.rewrite_complaint_professional(complaint_raw, log_cb=self.log_cb)

            cs = ComplaintState(
                complaint_id=cid,
                subject=subject,
                complaint_raw=complaint_raw,
                complaint_professional=prof,
                created_at=now_ts(),
                docs=[],
                evidence_pack_pdf=None,
                threads={},
                safe_mode=safe_mode,
                user_email=user_email,
                user_name=user_name,
                auto_send_policy=auto_send_policy,
            )
            self.complaints[cid] = cs
            self._save_state()
            self.log_cb(f"Added complaint {cid} (policy={auto_send_policy})")
            return cs

    def attach_docs(self, complaint_id: str, paths: List[str]):
        with self._lock:
            cs = self.complaints[complaint_id]
            for p in paths:
                if p and os.path.exists(p) and p not in cs.docs:
                    cs.docs.append(p)
            self._save_state()

    def build_evidence_pdf(self, complaint_id: str, out_path: str):
        with self._lock:
            cs = self.complaints[complaint_id]
            build_evidence_pack_pdf(out_path, complaint_id, cs.docs)
            cs.evidence_pack_pdf = out_path
            self._save_state()

    def list_complaints(self) -> List[ComplaintState]:
        with self._lock:
            return list(self.complaints.values())

    def get_complaint(self, complaint_id: str) -> ComplaintState:
        with self._lock:
            return self.complaints[complaint_id]

    # ---------- thread helpers ----------
    def _find_thread_by_label(self, cs: ComplaintState, label: str) -> Optional[str]:
        for tid, ts in cs.threads.items():
            if ts.label == label and ts.status not in ("abandoned", "resolved"):
                return tid
        return None

    def create_agent_thread_seed(
        self,
        complaint_id: str,
        agent_label: str,
        parent_thread_id: Optional[str],
        draft_email: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Auto-spawn a new agent thread by sending a seed email to TEST_INBOX_EMAIL.
        Subject contains [AGENT=...].
        """
        with self._lock:
            cs = self.complaints[complaint_id]

        subj = f"[{complaint_id}] [AGENT={agent_label}] {cs.subject}"

        body = f"""[AUTO-SPAWNED AGENT THREAD — TEST HARNESS]
Complaint: {complaint_id}
Agent: {agent_label}
Parent thread: {parent_thread_id or '(none)'}

--- ORIGINAL COMPLAINT (professional) ---
{cs.complaint_professional}
"""
        if draft_email:
            body += f"""

--- GPT DRAFT FOR THIS AGENT ---
Subject suggestion: {draft_email.get('subject','')}
{draft_email.get('body','')}
"""

        attachments: List[str] = []
        if cs.evidence_pack_pdf and os.path.exists(cs.evidence_pack_pdf):
            attachments.append(cs.evidence_pack_pdf)

        sent = send_email_with_attachments(
            service=self._gmail(),
            to_email=TEST_INBOX_EMAIL,
            subject=subj,
            body_text=body,
            attachments=attachments,
            thread_id=None,  # new thread
        )
        thread_id = sent.get("threadId")
        if not thread_id:
            raise RuntimeError("Gmail did not return threadId for spawned agent thread.")

        with self._lock:
            ts = ThreadState(
                thread_id=thread_id,
                label=agent_label,
                status="open",
                parent_thread_id=parent_thread_id,
                timeline=[],
                drafts=[],
            )
            # Make an initial draft available immediately
            initial_draft = {
                "kind": "email",
                "to_hint": agent_label,
                "subject": f"{cs.subject}",
                "body": cs.complaint_professional,
            }
            ts.drafts = [initial_draft]
            ts.last_draft = initial_draft
            ts.add_event("draft_ready", "Initial opening letter created from professional complaint")

            #ts.add_event("spawned", f"Spawned agent thread {agent_label} (to {TEST_INBOX_EMAIL})")
            cs.threads[thread_id] = ts
            self._save_state()

        self.log_cb(f"Spawned new thread {thread_id} for agent={agent_label}")
        return thread_id

    # ---------- SEND behavior (UI calls this) ----------
    def send_selected_draft_to_self(
        self,
        complaint_id: str,
        thread_id: str,
        draft_index: int,
    ):
        """
        SEND button behavior:
        - Send to TEST_INBOX_EMAIL (harness)
        - Put agent X into subject line as [AGENT=X]
        - Send into SAME thread (thread_id) to keep continuity
        """
        with self._lock:
            cs = self.complaints[complaint_id]
            ts = cs.threads[thread_id]
            drafts = ts.drafts or []
            if not drafts or draft_index < 0 or draft_index >= len(drafts):
                raise ValueError("Invalid draft index.")
            d = drafts[draft_index]

        agent = (d.get("to_hint") or ts.label or "unknown").strip()
        subject = (d.get("subject") or f"Re: {cs.subject}").strip()
        subject = f"[{complaint_id}] [AGENT={agent}] {subject}"

        body = (d.get("body") or "").strip()
        if not body:
            body = "(empty body)"

        attachments: List[str] = []
        if cs.evidence_pack_pdf and os.path.exists(cs.evidence_pack_pdf):
            attachments.append(cs.evidence_pack_pdf)
        attachments += [p for p in cs.docs if os.path.exists(p)]

        send_email_with_attachments(
            service=self._gmail(),
            to_email=TEST_INBOX_EMAIL,
            subject=subject,
            body_text=body,
            attachments=attachments,
            thread_id=thread_id,
        )

        with self._lock:
            ts.add_event("sent", f"Sent draft[{draft_index}] to {TEST_INBOX_EMAIL} agent={agent}")
            ts.last_outbound_sent_at = now_ts()
            self._save_state()

        self.log_cb(f"Sent draft to self: complaint={complaint_id} thread={thread_id} agent={agent}")

    # ---------- Phone call integration ----------
    def can_make_call(self, complaint_id: str, thread_id: str) -> bool:
        """Show the 'Make a call' UI button only after at least one email was sent in this thread."""
        with self._lock:
            cs = self.complaints.get(complaint_id)
            if not cs:
                return False
            ts = cs.threads.get(thread_id)
            if not ts:
                return False
            if ts.last_outbound_sent_at:
                return True
            # fallback: look for any 'sent' event in timeline
            for ev in (ts.timeline or []):
                if ev.get("kind") == "sent":
                    return True
            return False

    def make_support_call(
        self,
        complaint_id: str,
        thread_id: str,
        vendor_hint: Optional[str] = None,
        timeout_s: int = 300,
    ) -> str:
        """
        Places an automated support call using phone.call.ComplaintCallAgent and
        returns the customer-support reply as text (agent-only).

        This is intended to be invoked from Streamlit UI right after an email is sent.
        """
        with self._lock:
            cs = self.complaints[complaint_id]
            ts = cs.threads[thread_id]

            # Use the most informative text we have for the call.
            # Prefer raw complaint (more detail), fallback to professional complaint.
            user_complaint = (cs.complaint_raw or "").strip() or (cs.complaint_professional or "").strip()

        if not user_complaint:
            raise ValueError("Complaint text is empty; cannot place call.")

        # Lazy import so the manager can run even if phone deps aren't installed.
        try:
            from call import ComplaintCallAgent
        except Exception as e:
            raise RuntimeError(
                "Could not import phone.call.ComplaintCallAgent. "
                "Ensure your phone module is available as phone/call.py and dependencies are installed. "
                f"Import error: {e}"
            )

        agent = ComplaintCallAgent("config.ini")
        reply = agent.call_and_get_reply_autoroute(user_complaint, vendor_hint=vendor_hint, timeout=timeout_s)

        with self._lock:
            ts.last_phone_reply = {
                "ts": now_ts(),
                "vendor_hint": vendor_hint or "",
                "text": reply,
            }
            ts.add_event("phone_reply", "Captured customer-support reply via phone call")
            self._save_state()

        self.log_cb(f"Phone call completed: complaint={complaint_id} thread={thread_id} reply_len={len(reply)}")
        return reply

    # ---------- Auto-send policy enforcement ----------
    def _maybe_auto_send_thread(self, complaint_id: str, thread_id: str, reason: str):
        with self._lock:
            cs = self.complaints[complaint_id]
            ts = cs.threads[thread_id]
            policy = cs.auto_send_policy
            drafts = ts.drafts or []
            parent = ts.parent_thread_id

        if not drafts:
            return

        if policy == "off":
            return

        if policy == "spawned_only":
            # Only auto-send if this is a spawned thread
            if not parent:
                return

        if policy == "all":
            # Always auto-send first draft
            pass

        try:
            self.send_selected_draft_to_self(complaint_id, thread_id, 0)
            self.log_cb(f"AUTO-SENT draft[0] ({reason}) thread={thread_id}")
        except Exception as e:
            self.log_cb(f"ERROR: auto-send failed ({reason}) thread={thread_id}: {e}")

    # ---------- Polling loop ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.log_cb("Manager started polling.")

    def stop(self):
        self._running = False
        self.log_cb("Manager stopped polling.")

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                self.log_cb(f"ERROR: poll loop: {e}")
            for _ in range(self.poll_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_once(self):
        with self._lock:
            complaints = list(self.complaints.values())

        for cs in complaints:
            for thread_id, ts in list(cs.threads.items()):
                if ts.status in ("resolved", "abandoned"):
                    continue
                msg = self._newest_unprocessed_inbound(thread_id, ts.last_handled_msg_id)
                if msg:
                    self._handle_inbound(cs.complaint_id, thread_id, msg)

    def _newest_unprocessed_inbound(self, thread_id: str, last_handled_msg_id: Optional[str]) -> Optional[dict]:
        th = get_thread(self._gmail(), thread_id)
        messages = sorted(th.get("messages", []) or [], key=lambda m: int(m.get("internalDate", "0")))
        for m in reversed(messages):
            labels = m.get("labelIds") or []
            if self.processed_label_id in labels:
                continue
            if "SENT" in labels:
                continue
            if last_handled_msg_id and m.get("id") == last_handled_msg_id:
                continue
            return m
        return None


    def _newest_inbound_any(self, thread_id: str) -> Optional[dict]:
        """
        Returns newest inbound message in a thread, even if it was already marked processed.
        We still skip Gmail 'SENT' items (outbound from you).
        """
        th = get_thread(self._gmail(), thread_id)
        messages = sorted(th.get("messages", []) or [], key=lambda m: int(m.get("internalDate", "0")))
        for m in reversed(messages):
            labels = m.get("labelIds") or []
            if "SENT" in labels:
                continue
            return m
        return None

    def load_latest_inbound_view(self, complaint_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        UI helper: fetch latest inbound message text (does not mark processed and does not change last_handled_msg_id).
        Saves a cached copy into ThreadState.last_inbound for convenience.
        """
        with self._lock:
            cs = self.complaints[complaint_id]
            ts = cs.threads[thread_id]

        msg = self._newest_inbound_any(thread_id)
        if not msg:
            return None

        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        frm = get_header(headers, "From")
        subj = get_header(headers, "Subject")
        date = get_header(headers, "Date")
        labels = msg.get("labelIds") or []
        body = decode_best_effort_text(payload).strip()

        view = {
            "message_id": msg.get("id"),
            "from": frm,
            "subject": subj,
            "date": date,
            "labels": labels,
            "body": body,
        }

        with self._lock:
            ts.last_inbound = view
            ts.add_event("inbound_view", f"Viewed inbound msg={view.get('message_id')} subj={subj}")
            self._save_state()

        return view

    def draft_reply_now(self, complaint_id: str, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        UI helper: force an immediate GPT decision/draft from the newest *unprocessed* inbound.
        Equivalent to waiting for the poller, but on-demand.
        Returns the decision dict if a draft was created, otherwise None.
        """
        with self._lock:
            cs = self.complaints[complaint_id]
            ts = cs.threads[thread_id]

        msg = self._newest_unprocessed_inbound(thread_id, ts.last_handled_msg_id)
        if not msg:
            return None

        self._handle_inbound(complaint_id, thread_id, msg)
        with self._lock:
            return cs.threads[thread_id].last_decision

    def _thread_transcript(self, thread_id: str) -> str:
            th = get_thread(self._gmail(), thread_id)
            messages = sorted(th.get("messages", []) or [], key=lambda m: int(m.get("internalDate", "0")))
            out = []
            for m in messages:
                payload = m.get("payload", {}) or {}
                headers = payload.get("headers", []) or []
                subj = get_header(headers, "Subject")
                frm = get_header(headers, "From")
                date = get_header(headers, "Date")
                txt = decode_best_effort_text(payload).strip()
                out.append(f"---\nFrom: {frm}\nDate: {date}\nSubject: {subj}\n\n{txt}\n")
            return "\n".join(out)

    def _handle_inbound(self, complaint_id: str, thread_id: str, msg: dict):
            with self._lock:
                cs = self.complaints[complaint_id]
                ts = cs.threads[thread_id]

            transcript = self._thread_transcript(thread_id)

            payload = msg.get("payload", {}) or {}
            headers = payload.get("headers", []) or []
            frm = get_header(headers, "From")
            subj = get_header(headers, "Subject")

            user_data = {
                "available_docs": [os.path.basename(p) for p in cs.docs if os.path.exists(p)],
                "evidence_pack_pdf": os.path.basename(cs.evidence_pack_pdf) if cs.evidence_pack_pdf else None,
            }

            context = {
                "complaint_id": cs.complaint_id,
                "thread_label": ts.label,
                "thread_status": ts.status,
                "safe_mode": cs.safe_mode,
                "user_email": cs.user_email,
                "user_name": cs.user_name,
                "auto_send_policy": cs.auto_send_policy,
            }

            self.log_cb(f"Inbound detected: complaint={complaint_id} thread={thread_id} from={frm}")

            decision: AgentDecision = self.tp.decide_next(
                original_complaint=cs.complaint_professional,
                thread_transcript=transcript,
                user_data=user_data,
                context=context,
                log_cb=self.log_cb,
            )

            # Store decision + drafts on this thread
            with self._lock:
                ts.last_decision = asdict(decision)
                ts.drafts = list(decision.drafts or [])
                ts.last_draft = (ts.drafts[0] if ts.drafts else None)
                ts.status = decision.thread_status
                ts.add_event("inbound", f"From {frm} / subj={subj}")
                ts.add_event("decision", f"{decision.action} ({decision.confidence:.2f})")
                ts.last_handled_msg_id = msg.get("id")
                self._save_state()

            # Mark inbound as processed to avoid loops
            add_label(self._gmail(), msg["id"], self.processed_label_id)

            # Possibly auto-send on this thread
            self._maybe_auto_send_thread(complaint_id, thread_id, reason="inbound_decision")

            # AUTO-SPAWN new agent threads if GPT wants it
            if decision.action in ("spawn_mediator_agent", "escalate"):
                with self._lock:
                    cs = self.complaints[complaint_id]
                    current_label = cs.threads[thread_id].label

                seen = set()
                for d in (decision.drafts or []):
                    agent = (d.get("to_hint") or "").strip()
                    if not agent:
                        continue
                    if agent == current_label:
                        continue
                    if agent in seen:
                        continue
                    seen.add(agent)

                    existing_tid = self._find_thread_by_label(cs, agent)
                    if existing_tid:
                        with self._lock:
                            cs.threads[existing_tid].add_event("spawn_skipped", "Thread already exists for this agent label")
                            self._save_state()
                        continue

                    new_tid = self.create_agent_thread_seed(
                        complaint_id=complaint_id,
                        agent_label=agent,
                        parent_thread_id=thread_id,
                        draft_email={"subject": d.get("subject", ""), "body": d.get("body", "")},
                    )

                    # preload drafts into spawned thread
                    with self._lock:
                        new_ts = cs.threads[new_tid]
                        new_ts.drafts = [d]
                        new_ts.last_draft = d
                        new_ts.add_event("draft_ready", "Draft imported from parent decision")
                        self._save_state()

                    # Possibly auto-send spawned thread
                    self._maybe_auto_send_thread(complaint_id, new_tid, reason="spawned_thread")
