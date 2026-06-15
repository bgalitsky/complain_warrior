
# complaint_manager_phone_refactored.py
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import base64
import sqlite3
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
AUTO_SEND_POLICIES = ("manual", "draft_only", "auto_send")
DEFAULT_AUTO_SEND_POLICY = "manual"

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
        self.store = ComplaintStore(db_path=complaint_db_path or os.environ.get("CW_DB_PATH", "cw_store.sqlite"))
        self.call_store = CallResultStore(db_path=complaint_db_path or os.environ.get("CW_DB_PATH", "cw_store.sqlite"))
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

    def add_complaint(self, subject: str, complaint_raw: str, user_email: str, user_name: str, auto_send_policy: str = DEFAULT_AUTO_SEND_POLICY) -> ComplaintState:
        self._require_user()
        cid = f"CMP-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        prof = self.tp.rewrite_complaint_professional(complaint_raw, log_cb=self.log_cb)
        strat = self.tp.extract_resolution_strategy(prof, log_cb=self.log_cb)

        local_tid = f"LOCAL-company_support-{int(time.time()*1000)}"
        ts = ThreadState(thread_id=local_tid, label="company_support", status="open", stage="initial_demand", drafts=[], satisfaction=None)

        cs = ComplaintState(
            complaint_id=cid,
            subject=subject,
            complaint_raw=complaint_raw,
            complaint_professional=prof,
            user_email=user_email,
            user_name=user_name,
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
        ts = cs.threads[thread_id]
        strategy = ResolutionStrategy(**cs.strategy)
        self._ensure_gmail()
        combined = self._combine_transcript(cs, thread_id)
        user_data = {
            "available_docs": [os.path.basename(p) for p in cs.docs if os.path.exists(p)],
            "evidence_pack_pdf": os.path.basename(cs.evidence_pack_pdf) if cs.evidence_pack_pdf else None,
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

    def send_selected_drafts(self, complaint_id: str, thread_id: str, draft_indexes: List[int], attachments: Optional[List[str]] = None) -> List[dict]:
        cs = self.complaints[complaint_id]
        if self.should_pause_actions(cs):
            raise RuntimeError("Cannot send drafts while case is paused. " + self.pause_reason(cs))
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
            self._ensure_gmail()
            res = send_email_with_attachments(
                self.service,
                to_email=TEST_INBOX_EMAIL,
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
                {"thread_id": thread_id, "gmail_msg_id": res.get("id"), "gmail_thread_id": res.get("threadId"), "subject": subject}
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
        strategy_data = cs.strategy or {
            "primary_goal": "general resolution",
            "acceptable_fallbacks": [],
            "escalate_if": [],
            "evidence_needed": [],
        }
        strategy = ResolutionStrategy(**strategy_data)

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

    def place_phone_call_and_capture_reply(self, complaint_id: str, thread_id: str, timeout: int = 300) -> str:
        cs = self.complaints[complaint_id]
        if self.should_pause_actions(cs):
            raise RuntimeError("Cannot place phone call while case is paused. " + self.pause_reason(cs))
        ts = cs.threads[thread_id]
        if ComplaintCallAgent is None:
            raise RuntimeError("Phone module is not available (ComplaintCallAgent import failed).")

        agent = ComplaintCallAgent("config.ini")
        reply = agent.call_and_get_reply_autoroute(
            user_complaint=cs.complaint_raw or cs.complaint_professional,
            vendor_hint=None if ts.label in ("company_support", "agent") else ts.label,
            timeout=timeout,
            complaint_stage=ts.stage,
            current_status_summary=cs.current_status_summary,
        )
        reply = (reply or "").strip()

        self.call_store.set(
            f"{complaint_id}:{thread_id}:{int(time.time())}",
            {
                "transcript": reply,
                "thread_id": thread_id,
                "complaint_id": complaint_id,
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
                {"thread_id": thread_id, "transcript_empty": True},
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
            {"thread_id": thread_id, "transcript_empty": False},
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

