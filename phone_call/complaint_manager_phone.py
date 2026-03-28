
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

                # Otherwise continue workflow
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

        self.call_store.set(f"{complaint_id}:{thread_id}:{int(time.time())}", {"transcript": reply})
        self._append_activity(cs, "phone", "received", "Phone call reply", reply[:2000], {"thread_id": thread_id})
        resolved = self._apply_satisfaction(
            cs,
            ts,
            inbound_text=reply,
            trusted=(cs.auto_send_policy == "auto_send"),
        )

        if resolved:
            self._save(cs)
            return reply

        self.draft_reply_now(complaint_id, thread_id)
        self._save(cs)
        return reply

