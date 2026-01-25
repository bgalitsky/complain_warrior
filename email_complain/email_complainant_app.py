#!/usr/bin/env python3
"""
Tkinter Gmail + GPT-5 Complaint Assistant (SAFE TEST MODE)

What it does:
- Lets you create a complaint (subject/body)
- Uses GPT-5 to *discover likely real contact emails* (airline/regulator/etc.)
- Sends a TEST email to *yourself* (bgalitsky@hotmail.com) with discovered emails embedded in Subject:
    FORWARD_TO=...;CC=...;TAG=CASE-... | <your subject>
- Monitors that Gmail thread for *inbound* replies (ignores your SENT drafts to prevent loops)
- Uses GPT-5 to draft next replies
- Lets you add/drop supporting documents (receipts, PDFs) and attach them to outgoing messages
- By default sends drafts to *yourself* for review (recommended)

Requirements:
  pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib openai

Files expected:
  credentials.json   (OAuth client secrets from Google Cloud)
  token.json         (will be created after first auth)
  OPENAI_API_KEY env var must be set

Optional drag&drop:
  pip install tkinterdnd2
  (If not installed, app will fall back to "Add files..." button.)
"""

import os
import re
import json
import time
import base64
import mimetypes
import threading
from configparser import RawConfigParser
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from openai import OpenAI

config_path='config.ini'
config = RawConfigParser()
config.read(config_path)

# Facebook credentials
email = config.get('Facebook', 'email')
password = config.get('Facebook', 'password')

# API keys
gemini_api_key = config.get('Gemini', 'api_key')
os.environ["OPENAI_API_KEY"] = config.get('OpenAI', 'api_key')

# -----------------------------
# CONFIG
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_JSON = "credentials.json"
TOKEN_JSON = "token.json"
STATE_FILE = "discovery_state.json"

OPENAI_MODEL = "gpt-5"
YOUR_EMAIL = "bgalitsky@hotmail.com"
YOUR_NAME = "Boris Galitsky"

POLL_INTERVAL_SECONDS = 60

# Labels used to prevent reprocessing
LABEL_PROCESSED = "AUTO_DISCOVERY_PROCESSED"

client = OpenAI()

# -----------------------------
# Optional TkinterDnD
# -----------------------------
HAS_DND = False
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    HAS_DND = True
except Exception:
    HAS_DND = False


# -----------------------------
# LLM prompts
# -----------------------------
DISCOVERY_SYSTEM = """You help discover likely contact email addresses for handling a customer complaint.
Return STRICT JSON ONLY with:
- parties: array of objects:
  - name: string (e.g., "Southwest Customer Relations", "US DOT Aviation Consumer Protection")
  - role: one of ["airline_support","regulator","consumer_protection","other"]
  - emails: array of strings (email addresses ONLY)
  - confidence: number 0..1
  - notes: short string (max 1 sentence)
Rules:
- Provide 1-3 emails per party.
- If unsure, still propose candidates but set confidence <= 0.5.
- Do NOT include URLs, phones, or postal addresses.
- It's OK to return plausible candidates; accuracy is not guaranteed.
"""

REPLY_SYSTEM = """You help a customer respond to an inbound email about their complaint.

You will be given:
- Intended recipient (from metadata; may be a domain/email)
- Original complaint
- Latest inbound email text
- Optional list of documents available (filenames)

Return STRICT JSON with:
- outcome: one of ["accepted","partial_accept","denied","needs_more_info","generic","other"]
- confidence: number 0..1
- reply_subject: string
- reply_body: string

Constraints:
- Keep it calm, factual, and concise.
- If denied: ask for re-evaluation, cite evidence available, ask for policy basis and next steps.
- If needs_more_info: provide requested details, ask preferred upload method/format.
- Do NOT mention an airline/company name unless it appears in the inbound email OR is strongly implied by intended recipient domain/email.
- Do NOT threaten, harass, or use profanity.
"""


# -----------------------------
# Gmail helpers
# -----------------------------
def get_gmail_creds():
    creds = None
    if os.path.exists(TOKEN_JSON):
        creds = Credentials.from_authorized_user_file(TOKEN_JSON, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_JSON):
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_JSON}. Download OAuth client JSON and rename to credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_JSON, SCOPES)
            # For headless: creds = flow.run_console()
            creds = flow.run_local_server(port=0)
        with open(TOKEN_JSON, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def build_gmail_service():
    creds = get_gmail_creds()
    return build("gmail", "v1", credentials=creds)


def get_header(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def decode_plain_text(payload) -> str:
    if not payload:
        return ""
    body = payload.get("body", {}) or {}
    data = body.get("data")
    if data:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")

    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            pdata = (part.get("body") or {}).get("data")
            if pdata:
                return base64.urlsafe_b64decode(pdata.encode("utf-8")).decode("utf-8", errors="replace")

    # fallback: any text/*
    for part in payload.get("parts", []) or []:
        if (part.get("mimeType") or "").startswith("text/"):
            pdata = (part.get("body") or {}).get("data")
            if pdata:
                return base64.urlsafe_b64decode(pdata.encode("utf-8")).decode("utf-8", errors="replace")

    return ""


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
    service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()


def send_email_with_attachments(
    service,
    to_email: str,
    subject: str,
    body_text: str,
    attachments: Optional[List[str]] = None,
    thread_id: Optional[str] = None,
    in_reply_to: str = "",
    references: str = "",
):
    """
    Sends a message (optionally in-thread) with optional file attachments.
    """
    attachments = attachments or []

    msg = MIMEMultipart()
    msg["To"] = to_email
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

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


def get_thread(service, thread_id: str) -> dict:
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


def find_seed_subject_in_thread(service, thread_id: str) -> str:
    """
    Find the original seed message subject containing FORWARD_TO=... (directive).
    """
    th = get_thread(service, thread_id)
    messages = th.get("messages", []) or []
    for m in messages:
        payload = m.get("payload", {})
        headers = payload.get("headers", []) or []
        subj = get_header(headers, "Subject") or ""
        if "FORWARD_TO=" in subj:
            return subj
    return ""


def parse_forward_directive(subject: str) -> Dict[str, List[str]]:
    """
    Parses:
      FORWARD_TO=a@b.com,c@d.com;CC=e@f.com;TAG=CASE-... | ...
    """
    out = {"forward_to": [], "cc": [], "tag": []}
    if not subject:
        return out

    directive = subject.split(" | ", 1)[0]

    m1 = re.search(r"FORWARD_TO=([^;]+)", directive, re.IGNORECASE)
    if m1:
        out["forward_to"] = [x.strip() for x in m1.group(1).split(",") if x.strip() and x.strip() != "UNKNOWN"]

    m2 = re.search(r"CC=([^;]+)", directive, re.IGNORECASE)
    if m2:
        out["cc"] = [x.strip() for x in m2.group(1).split(",") if x.strip() and x.strip() != "UNKNOWN"]

    m3 = re.search(r"TAG=([^;]+)", directive, re.IGNORECASE)
    if m3:
        out["tag"] = [m3.group(1).strip()]

    return out


def newest_unprocessed_inbound_message(service, thread_id: str, processed_label_id: str, last_handled_msg_id: Optional[str]) -> Optional[dict]:
    """
    Returns newest message in thread that:
      - is NOT SENT (so we don't loop on our own drafts)
      - is NOT already labeled processed
      - is NOT the same as last_handled_msg_id
    """
    th = get_thread(service, thread_id)
    messages = th.get("messages", []) or []
    # sort by internalDate ascending
    messages = sorted(messages, key=lambda m: int(m.get("internalDate", "0")))

    for m in reversed(messages):  # newest -> oldest
        labels = m.get("labelIds") or []
        if processed_label_id in labels:
            continue
        if "SENT" in labels:
            continue  # critical: ignore our own outgoing mail
        if last_handled_msg_id and m.get("id") == last_handled_msg_id:
            continue
        return m

    return None


# -----------------------------
# LLM calls
# -----------------------------
def llm_discover_emails(human_subject: str, complaint_body: str) -> dict:
    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": DISCOVERY_SYSTEM},
            {"role": "user", "content": f"SUBJECT:\n{human_subject}\n\nCOMPLAINT:\n{complaint_body}\n\nIdentify relevant parties and their likely contact emails."}
        ],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(resp.output_text)


def llm_draft_reply(intended_recipient: str, original_complaint: str, inbound_text: str, doc_names: List[str]) -> dict:
    docs_str = ", ".join(doc_names) if doc_names else "(none)"
    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": REPLY_SYSTEM},
            {"role": "user", "content": f"INTENDED RECIPIENT:\n{intended_recipient}\n\nAVAILABLE DOCS:\n{docs_str}\n\nORIGINAL COMPLAINT:\n{original_complaint}\n\nLATEST INBOUND EMAIL:\n{inbound_text}\n\nDraft the next reply as JSON."}
        ],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(resp.output_text)


# -----------------------------
# State
# -----------------------------
@dataclass
class CaseState:
    tag: str
    thread_id: str
    seed_subject: str
    human_subject: str
    complaint_body: str
    forward_to: List[str]
    cc: List[str]
    last_handled_msg_id: Optional[str] = None
    created_at: str = ""
    docs: List[str] = None  # file paths

    def to_json(self) -> dict:
        d = asdict(self)
        d["docs"] = d["docs"] or []
        return d

    @staticmethod
    def from_json(d: dict, fallback_tag: str = "") -> "CaseState":
        tag = d.get("tag") or fallback_tag
        if not tag:
            raise ValueError("CaseState missing 'tag' and no fallback_tag provided.")

        return CaseState(
            tag=tag,
            thread_id=d.get("thread_id", ""),
            seed_subject=d.get("seed_subject", ""),
            human_subject=d.get("human_subject", ""),
            complaint_body=d.get("complaint_body", ""),
            forward_to=d.get("forward_to", []) or [],
            cc=d.get("cc", []) or [],
            last_handled_msg_id=d.get("last_handled_msg_id"),
            created_at=d.get("created_at", ""),
            docs=d.get("docs", []) or [],
        )

def load_state() -> Dict[str, dict]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: Dict[str, dict]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# -----------------------------
# Tkinter app
# -----------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Complaint Email Assistant (Gmail + GPT-5)")

        self.service = None
        self.processed_label_id = None

        self.state = load_state()
        self.current_case: Optional[CaseState] = None

        self.monitoring = False
        self.monitor_thread = None

        self._build_ui()
        self._init_gmail()

        self._refresh_case_list()

    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)

        self.tab_new = ttk.Frame(self.nb)
        self.tab_cases = ttk.Frame(self.nb)

        self.nb.add(self.tab_new, text="New Case")
        self.nb.add(self.tab_cases, text="Cases & Monitor")

        # --- New Case UI ---
        frm = ttk.Frame(self.tab_new, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Complaint subject").grid(row=0, column=0, sticky="w")
        self.ent_subject = ttk.Entry(frm, width=90)
        self.ent_subject.grid(row=1, column=0, columnspan=3, sticky="we", pady=(0, 8))
        self.ent_subject.insert(0, "Complaint: Flight delay reimbursement request (Reservation Q7K2LM)")

        ttk.Label(frm, text="Complaint body").grid(row=2, column=0, sticky="w")
        self.txt_body = tk.Text(frm, height=12, width=100)
        self.txt_body.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        self.txt_body.insert("1.0", f"""Dear Customer Relations,

I am writing to file a formal complaint regarding a recent flight disruption.

Summary:
- Flight delayed by more than 4 hours (operational issues).
- I missed a prepaid hotel reservation and an important meeting.
- No meaningful assistance was offered at the airport.

Requested remedy:
1) Reimbursement for hotel cancellation fee: $180
2) A reasonable travel credit for the disruption

I can provide receipts and confirmation emails upon request.

Sincerely,
{YOUR_NAME}
{YOUR_EMAIL}
Reservation: Q7K2LM
""")

        self.btn_discover = ttk.Button(frm, text="1) Discover emails (GPT-5)", command=self.on_discover)
        self.btn_discover.grid(row=4, column=0, sticky="w")

        self.btn_send_seed = ttk.Button(frm, text="2) Send test email to myself", command=self.on_send_seed, state="disabled")
        self.btn_send_seed.grid(row=4, column=1, sticky="w", padx=8)

        ttk.Label(frm, text="Discovery output / recipients (editable JSON)").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.txt_discovery = tk.Text(frm, height=10, width=100)
        self.txt_discovery.grid(row=6, column=0, columnspan=3, sticky="nsew")

        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(3, weight=1)
        frm.rowconfigure(6, weight=1)

        # --- Cases/Monitor UI ---
        frm2 = ttk.Frame(self.tab_cases, padding=10)
        frm2.pack(fill="both", expand=True)

        left = ttk.Frame(frm2)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="Cases").pack(anchor="w")
        self.lst_cases = tk.Listbox(left, width=40, height=18)
        self.lst_cases.pack(fill="y", expand=True)
        self.lst_cases.bind("<<ListboxSelect>>", self.on_case_select)

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Reload", command=self._refresh_case_list).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.on_delete_case).pack(side="left", padx=6)

        right = ttk.Frame(frm2)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        # Case details
        self.lbl_case = ttk.Label(right, text="Select a case", font=("Segoe UI", 10, "bold"))
        self.lbl_case.pack(anchor="w")

        meta = ttk.Frame(right)
        meta.pack(fill="x", pady=6)
        self.lbl_to = ttk.Label(meta, text="FORWARD_TO: -")
        self.lbl_to.pack(anchor="w")
        self.lbl_cc = ttk.Label(meta, text="CC: - (optional)")
        self.lbl_cc.pack(anchor="w")

        # Documents area
        docs = ttk.LabelFrame(right, text="Supporting documents (receipts, PDFs, screenshots)")
        docs.pack(fill="x", pady=8)

        self.lst_docs = tk.Listbox(docs, height=5)
        self.lst_docs.pack(fill="x", padx=8, pady=(8, 4))

        doc_btns = ttk.Frame(docs)
        doc_btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(doc_btns, text="Add files...", command=self.on_add_docs).pack(side="left")
        ttk.Button(doc_btns, text="Remove selected", command=self.on_remove_doc).pack(side="left", padx=6)

        if HAS_DND:
            ttk.Label(docs, text="Tip: You can drag & drop files onto the list above.").pack(anchor="w", padx=8)
            # DND binding will be set in _enable_dnd()

        # Draft area
        draft = ttk.LabelFrame(right, text="Latest inbound + Draft reply (GPT-5)")
        draft.pack(fill="both", expand=True, pady=8)

        self.txt_inbound = tk.Text(draft, height=8)
        self.txt_inbound.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.txt_inbound.insert("1.0", "")

        self.txt_draft = tk.Text(draft, height=10)
        self.txt_draft.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.txt_draft.insert("1.0", "")

        actions = ttk.Frame(right)
        actions.pack(fill="x")

        self.btn_start = ttk.Button(actions, text="Start monitoring", command=self.on_start_monitor, state="disabled")
        self.btn_start.pack(side="left")

        self.btn_stop = ttk.Button(actions, text="Stop", command=self.on_stop_monitor, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        self.send_to_self_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="Send drafts to myself (recommended)", variable=self.send_to_self_var).pack(side="left", padx=10)

        self.btn_send_draft = ttk.Button(actions, text="Send draft now", command=self.on_send_draft_now, state="disabled")
        self.btn_send_draft.pack(side="right")

        if HAS_DND:
            self._enable_dnd()

    def _enable_dnd(self):
        # DND support requires root to be TkinterDnD.Tk instance; if not, skip gracefully.
        try:
            self.lst_docs.drop_target_register(DND_FILES)  # type: ignore
            self.lst_docs.dnd_bind("<<Drop>>", self._on_drop_files)  # type: ignore
        except Exception:
            pass

    def _on_drop_files(self, event):
        # event.data may contain one or many paths, possibly wrapped in {}
        raw = event.data
        paths = self._parse_dnd_paths(raw)
        if paths:
            self._add_docs(paths)

    @staticmethod
    def _parse_dnd_paths(data: str) -> List[str]:
        # Windows: '{C:\\path one\\a.pdf}' '{C:\\path\\b.png}'
        paths = []
        token = ""
        in_brace = False
        for ch in data:
            if ch == "{":
                in_brace = True
                token = ""
            elif ch == "}":
                in_brace = False
                if token.strip():
                    paths.append(token.strip())
                token = ""
            elif ch == " " and not in_brace:
                if token.strip():
                    paths.append(token.strip())
                    token = ""
            else:
                token += ch
        if token.strip():
            paths.append(token.strip())
        # dedupe
        out = []
        for p in paths:
            p = p.strip('"')
            if p and p not in out:
                out.append(p)
        return out

    def _init_gmail(self):
        try:
            self.service = build_gmail_service()
            self.processed_label_id = ensure_label(self.service, LABEL_PROCESSED)
        except Exception as e:
            messagebox.showerror("Gmail init failed", str(e))

    def _refresh_case_list(self):
        self.state = load_state()
        self.lst_cases.delete(0, tk.END)
        for tag, d in sorted(self.state.items(), key=lambda x: x[0], reverse=True):
            subj = d.get("human_subject", "")
            self.lst_cases.insert(tk.END, f"{tag} | {subj}")
        self._clear_case_view()

    def _clear_case_view(self):
        self.current_case = None
        self.lbl_case.config(text="Select a case")
        self.lbl_to.config(text="FORWARD_TO: -")
        self.lbl_cc.config(text="CC: -")
        self.lst_docs.delete(0, tk.END)
        self.txt_inbound.delete("1.0", tk.END)
        self.txt_draft.delete("1.0", tk.END)
        self.btn_start.config(state="disabled")
        self.btn_send_draft.config(state="disabled")

    def on_discover(self):
        subj = self.ent_subject.get().strip()
        body = self.txt_body.get("1.0", tk.END).strip()
        if not subj or not body:
            messagebox.showwarning("Missing info", "Please provide subject and body.")
            return

        self.btn_discover.config(state="disabled")
        self.root.update_idletasks()

        try:
            discovery = llm_discover_emails(subj, body)
        except Exception as e:
            messagebox.showerror("Discovery failed", str(e))
            self.btn_discover.config(state="normal")
            return

        # Pretty-print in editor
        self.txt_discovery.delete("1.0", tk.END)
        self.txt_discovery.insert("1.0", json.dumps(discovery, ensure_ascii=False, indent=2))
        self.btn_send_seed.config(state="normal")
        self.btn_discover.config(state="normal")

    def on_send_seed(self):
        if not self.service:
            messagebox.showerror("No Gmail", "Gmail service not initialized.")
            return

        subj = self.ent_subject.get().strip()
        body = self.txt_body.get("1.0", tk.END).strip()
        disc_text = self.txt_discovery.get("1.0", tk.END).strip()
        if not disc_text:
            messagebox.showwarning("No discovery", "Run discovery first.")
            return

        try:
            discovery = json.loads(disc_text)
        except Exception:
            messagebox.showerror("Invalid JSON", "Discovery JSON is not valid.")
            return

        parties = discovery.get("parties", []) or []

        # Choose top candidates for directive fields (keep small)
        forward_to = []
        cc = []
        for p in parties:
            emails = p.get("emails", []) or []
            if not emails:
                continue
            role = p.get("role", "")
            if role == "airline_support" and len(forward_to) < 2:
                forward_to.append(emails[0])
            elif role in ("regulator", "consumer_protection") and len(cc) < 2:
                cc.append(emails[0])

        if not forward_to:
            forward_to = ["UNKNOWN"]
        if not cc:
            cc = []

        case_tag = f"CASE-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        directive = f"FORWARD_TO={','.join(forward_to)};CC={','.join(cc) if cc else ''};TAG={case_tag}"
        seed_subject = f"{directive} | {subj}"

        discovery_block = json.dumps(discovery, ensure_ascii=False, indent=2)

        seed_body = f"""[TEST MODE — NOT SENT TO DISCOVERED ADDRESSES]
This email was generated to test GPT-5 email discovery safely.

INTENDED RECIPIENTS (from subject directive):
FORWARD_TO: {', '.join(forward_to)}
CC: {', '.join(cc) if cc else '(none)'}
TAG: {case_tag}

LLM DISCOVERY OUTPUT:
{discovery_block}

--- ORIGINAL COMPLAINT ---
Subject: {subj}

{body}
"""

        try:
            sent = send_email_with_attachments(
                service=self.service,
                to_email=YOUR_EMAIL,
                subject=seed_subject,
                body_text=seed_body,
                attachments=[],
            )
        except Exception as e:
            messagebox.showerror("Send failed", str(e))
            return

        thread_id = sent.get("threadId")
        if not thread_id:
            messagebox.showerror("Send failed", "No threadId returned.")
            return

        # Save case state
        cs = CaseState(
            tag=case_tag,
            thread_id=thread_id,
            seed_subject=seed_subject,
            human_subject=subj,
            complaint_body=body,
            forward_to=[x for x in forward_to if x != "UNKNOWN"],
            cc=cc,
            last_handled_msg_id=None,
            created_at=datetime.now().isoformat(timespec="seconds"),
            docs=[],
        )

        st = load_state()
        st[case_tag] = cs.to_json()
        save_state(st)

        messagebox.showinfo("Seed sent", f"Sent test email to {YOUR_EMAIL}\nCase: {case_tag}")
        self._refresh_case_list()
        self.nb.select(self.tab_cases)

    def on_case_select(self, event=None):
        sel = self.lst_cases.curselection()
        if not sel:
            return
        line = self.lst_cases.get(sel[0])
        tag = line.split(" | ", 1)[0].strip()
        d = self.state.get(tag)
        if not d:
            return
        #self.current_case = CaseState.from_json(d)
        self.current_case = CaseState.from_json(d, fallback_tag=tag)
        self.lbl_case.config(text=f"{self.current_case.tag} | {self.current_case.human_subject}")
        self.lbl_to.config(text=f"FORWARD_TO: {', '.join(self.current_case.forward_to) if self.current_case.forward_to else '(unknown)'}")
        self.lbl_cc.config(text=f"CC: {', '.join(self.current_case.cc) if self.current_case.cc else '(none)'}")

        self.lst_docs.delete(0, tk.END)
        for p in self.current_case.docs or []:
            self.lst_docs.insert(tk.END, p)

        self.txt_inbound.delete("1.0", tk.END)
        self.txt_draft.delete("1.0", tk.END)

        self.btn_start.config(state="normal")
        self.btn_send_draft.config(state="normal")

    def on_delete_case(self):
        sel = self.lst_cases.curselection()
        if not sel:
            return
        line = self.lst_cases.get(sel[0])
        tag = line.split(" | ", 1)[0].strip()
        if messagebox.askyesno("Delete", f"Delete case {tag}?"):
            st = load_state()
            if tag in st:
                del st[tag]
                save_state(st)
            self._refresh_case_list()

    def on_add_docs(self):
        paths = filedialog.askopenfilenames(title="Select documents")
        if paths:
            self._add_docs(list(paths))

    def _add_docs(self, paths: List[str]):
        if not self.current_case:
            messagebox.showwarning("No case", "Select a case first.")
            return
        # add unique existing paths
        cur = self.current_case.docs or []
        changed = False
        for p in paths:
            if p and os.path.exists(p) and p not in cur:
                cur.append(p)
                changed = True
        if changed:
            self.current_case.docs = cur
            self._persist_current_case()
            self.on_case_select()

    def on_remove_doc(self):
        if not self.current_case:
            return
        sel = self.lst_docs.curselection()
        if not sel:
            return
        idx = sel[0]
        docs = self.current_case.docs or []
        if idx < len(docs):
            docs.pop(idx)
            self.current_case.docs = docs
            self._persist_current_case()
            self.on_case_select()

    def _persist_current_case(self):
        if not self.current_case:
            return
        st = load_state()
        st[self.current_case.tag] = self.current_case.to_json()
        save_state(st)
        self.state = st

    def on_start_monitor(self):
        if not self.current_case or not self.service:
            return
        if self.monitoring:
            return
        self.monitoring = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

        # Run monitor loop in background thread, update UI via root.after
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def on_stop_monitor(self):
        self.monitoring = False
        self.btn_stop.config(state="disabled")
        self.btn_start.config(state="normal")

    def _monitor_loop(self):
        """
        Polls the selected case thread. When a new inbound message appears, drafts a reply and shows it.
        Also sends a draft-to-self if you click "Send draft now" (manual).
        """
        while self.monitoring and self.current_case:
            try:
                cs = self.current_case

                # Ensure we have a directive subject (in case state was old)
                if not cs.seed_subject:
                    cs.seed_subject = find_seed_subject_in_thread(self.service, cs.thread_id)
                    self._persist_current_case()

                intended = cs.forward_to[0] if cs.forward_to else "(unknown)"

                msg = newest_unprocessed_inbound_message(
                    service=self.service,
                    thread_id=cs.thread_id,
                    processed_label_id=self.processed_label_id,
                    last_handled_msg_id=cs.last_handled_msg_id,
                )
                if msg:
                    payload = msg.get("payload", {})
                    inbound_text = decode_plain_text(payload).strip()
                    headers = payload.get("headers", []) or []
                    subj = get_header(headers, "Subject") or cs.seed_subject or cs.human_subject

                    # Draft with LLM
                    doc_names = [os.path.basename(p) for p in (cs.docs or []) if os.path.exists(p)]
                    draft = llm_draft_reply(intended, cs.complaint_body, inbound_text, doc_names)

                    # Update UI
                    def ui_update():
                        self.txt_inbound.delete("1.0", tk.END)
                        self.txt_inbound.insert("1.0", f"Subject: {subj}\n\n{inbound_text}")

                        self.txt_draft.delete("1.0", tk.END)
                        body = draft.get("reply_body", "")
                        self.txt_draft.insert(
                            "1.0",
                            f"Outcome: {draft.get('outcome')} (conf {draft.get('confidence')})\n\n"
                            f"Suggested subject: {draft.get('reply_subject')}\n\n"
                            f"{body}"
                        )

                    self.root.after(0, ui_update)

                    # Mark processed + update last handled message id
                    add_label(self.service, msg["id"], self.processed_label_id)
                    cs.last_handled_msg_id = msg["id"]
                    self._persist_current_case()

            except Exception as e:
                def ui_err():
                    messagebox.showerror("Monitor error", str(e))
                self.root.after(0, ui_err)
                self.monitoring = False
                break

            # sleep
            for _ in range(POLL_INTERVAL_SECONDS):
                if not self.monitoring:
                    break
                time.sleep(1)

        # cleanup buttons
        def ui_done():
            self.btn_stop.config(state="disabled")
            self.btn_start.config(state="normal")
        self.root.after(0, ui_done)

    def on_send_draft_now(self):
        """
        Sends the draft email to yourself (default) in the same thread, with any attached docs.
        This does NOT auto-send to discovered parties (you remain in control).
        """
        if not self.current_case or not self.service:
            return

        # Extract suggested subject/body from draft box
        draft_text = self.txt_draft.get("1.0", tk.END).strip()
        if not draft_text:
            messagebox.showwarning("No draft", "No draft available.")
            return

        # crude parse: look for "Suggested subject: ..."
        m = re.search(r"Suggested subject:\s*(.+)", draft_text)
        suggested_subject = m.group(1).strip() if m else f"Re: {self.current_case.human_subject}"

        # Body: everything after the second blank line is not reliable; instead ask LLM output again?
        # We'll take the last part after the last blank line following suggested subject marker.
        # Safer: use the content after "Suggested subject:" line.
        parts = draft_text.split("Suggested subject:", 1)
        body_part = parts[1] if len(parts) == 2 else draft_text
        # Remove first line (subject line)
        body_lines = body_part.splitlines()
        body_lines = body_lines[1:] if len(body_lines) > 1 else body_lines
        reply_body = "\n".join(body_lines).strip()

        # Add a short header listing attached docs
        docs = [p for p in (self.current_case.docs or []) if os.path.exists(p)]
        if docs:
            reply_body = (
                reply_body
                + "\n\nAttachments included:\n"
                + "\n".join([f"- {os.path.basename(p)}" for p in docs])
            )

        to_email = YOUR_EMAIL if self.send_to_self_var.get() else YOUR_EMAIL  # keep safe default
        # (If you later want a "Send to intended recipient" mode, wire it here.)

        try:
            # send in-thread to yourself
            send_email_with_attachments(
                service=self.service,
                to_email=to_email,
                subject=suggested_subject,
                body_text="[DRAFT RESPONSE — REVIEW BEFORE SENDING]\n\n" + reply_body,
                attachments=docs,
                thread_id=self.current_case.thread_id,
            )
            messagebox.showinfo("Sent", f"Draft sent to {to_email} with {len(docs)} attachment(s).")
        except Exception as e:
            messagebox.showerror("Send failed", str(e))


def main():
    if HAS_DND:
        # If TkinterDnD2 exists, root must be created from TkinterDnD.Tk
        root = TkinterDnD.Tk()  # type: ignore
    else:
        root = tk.Tk()
    App(root)
    root.geometry("1100x780")
    root.mainloop()


if __name__ == "__main__":
    main()
