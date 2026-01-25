import os
import json
import time
import base64
import re
from configparser import RawConfigParser
from datetime import datetime
from typing import List, Dict, Optional

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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

OPENAI_MODEL = "gpt-5"
client = OpenAI()

YOUR_EMAIL = "bgalitsky@hotmail.com"
YOUR_NAME = "Boris Galitsky"

POLL_INTERVAL_SECONDS = 60
MAX_POLLS = 240  # 4 hours
STATE_FILE = "discovery_state.json"
LABEL_PROCESSED = "AUTO_DISCOVERY_PROCESSED"

# -----------------------------
# COMPLAINT INPUT (example)
# -----------------------------
HUMAN_SUBJECT = "Complaint: Flight delay reimbursement request (Reservation Q7K2LM)"
COMPLAINT_BODY = f"""Dear Customer Relations,

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
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_JSON, SCOPES)
            # For headless servers: creds = flow.run_console()
            creds = flow.run_local_server(port=0)
        with open(TOKEN_JSON, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def get_header(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
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


def send_to_self(service, subject: str, body_text: str):
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["To"] = YOUR_EMAIL
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_thread_reply(service, thread_id: str, to_addr: str, subject: str, body: str,
                      in_reply_to: str = "", references: str = ""):
    mime = MIMEMultipart()
    mime["To"] = to_addr
    mime["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
    if references:
        mime["References"] = references
    mime.attach(MIMEText(body, "plain", "utf-8"))
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()


# -----------------------------
# State
# -----------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# -----------------------------
# LLM: discover "real" emails (test-only)
# -----------------------------
DISCOVERY_SYSTEM = """You are helping discover likely contact email addresses for handling a customer complaint.
Return STRICT JSON ONLY with:
- parties: array of objects with fields:
  - name: string (e.g., "Southwest Customer Relations", "US DOT Aviation Consumer Protection")
  - role: one of ["airline_support","regulator","consumer_protection","other"]
  - emails: array of strings (email addresses)
  - confidence: number 0..1 (your confidence these emails are correct)
  - notes: short string (max 1 sentence)
Rules:
- Provide 1-3 emails per party.
- Prefer official-looking addresses and known patterns.
- If you are unsure, still propose candidates but set confidence <= 0.5.
- Do NOT include phone numbers or URLs, only emails.
"""

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


# -----------------------------
# Parse intended recipients from subject
# -----------------------------
def parse_forward_to(subject: str) -> Dict[str, List[str]]:
    """
    Parses:
      FORWARD_TO=a@b.com,c@d.com;CC=e@f.com;TAG=CASE-... | ...
    """
    out = {"forward_to": [], "cc": [], "tag": []}
    if not subject:
        return out

    # Extract the directive segment before " | "
    directive = subject.split(" | ", 1)[0]

    m1 = re.search(r"FORWARD_TO=([^;]+)", directive, re.IGNORECASE)
    if m1:
        out["forward_to"] = [x.strip() for x in m1.group(1).split(",") if x.strip()]

    m2 = re.search(r"CC=([^;]+)", directive, re.IGNORECASE)
    if m2:
        out["cc"] = [x.strip() for x in m2.group(1).split(",") if x.strip()]

    m3 = re.search(r"TAG=([^;]+)", directive, re.IGNORECASE)
    if m3:
        out["tag"] = [m3.group(1).strip()]

    return out


# -----------------------------
# LLM: draft replies based on inbound email (you forwarded/replied manually)
# -----------------------------
REPLY_SYSTEM = """You help a customer respond to an inbound email about their complaint.
Return STRICT JSON with:
- outcome: one of ["accepted","partial_accept","denied","needs_more_info","generic","other"]
- confidence: number 0..1
- reply_subject: string
- reply_body: string

Keep replies calm, factual, and concise.
If denied: request re-evaluation, ask for policy basis, cite evidence available, propose next steps.
If needs_more_info: provide requested details and ask what documentation format is acceptable.
"""

def llm_draft_next(original_complaint: str, inbound_text: str, intended_recipient: str) -> dict:
    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": REPLY_SYSTEM},
            {"role": "user", "content": f"INTENDED RECIPIENT (from subject): {intended_recipient}\n\nORIGINAL COMPLAINT:\n{original_complaint}\n\nINBOUND EMAIL:\n{inbound_text}\n\nDraft the next reply."}
        ],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(resp.output_text)


# -----------------------------
# MAIN
# -----------------------------
def main():
    creds = get_gmail_creds()
    service = build("gmail", "v1", credentials=creds)
    processed_label_id = ensure_label(service, LABEL_PROCESSED)

    # 1) Discover emails (test-only)
    discovery = llm_discover_emails(HUMAN_SUBJECT, COMPLAINT_BODY)
    parties = discovery.get("parties", [])

    # 2) Build machine-readable subject with discovered emails
    case_id = f"CASE-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    # Flatten top candidates into forward_to + cc (keep small)
    forward_to = []
    cc = []
    for p in parties:
        emails = p.get("emails", [])
        if not emails:
            continue
        if p.get("role") == "airline_support" and len(forward_to) < 2:
            forward_to.extend(emails[:1])
        elif p.get("role") in ("regulator", "consumer_protection") and len(cc) < 2:
            cc.extend(emails[:1])

    # If nothing found, still create a test message
    forward_to_str = ",".join(forward_to) if forward_to else "UNKNOWN"
    cc_str = ",".join(cc) if cc else ""

    subject = f"FORWARD_TO={forward_to_str};CC={cc_str};TAG={case_id} | {HUMAN_SUBJECT}"

    # Include full discovery list in body for manual review
    discovery_block = json.dumps(discovery, ensure_ascii=False, indent=2)

    test_email_body = f"""[TEST MODE — NOT SENT TO DISCOVERED ADDRESSES]
This email was generated to test LLM email discovery safely.

INTENDED RECIPIENTS (from subject):
FORWARD_TO: {forward_to_str}
CC: {cc_str}
TAG: {case_id}

LLM DISCOVERY OUTPUT:
{discovery_block}

--- ORIGINAL COMPLAINT ---
Subject: {HUMAN_SUBJECT}

{COMPLAINT_BODY}
"""

    sent = send_to_self(service, subject, test_email_body)
    thread_id = sent.get("threadId")
    print("Sent discovery test email to self.")
    print("Case TAG:", case_id)
    print("Thread ID:", thread_id)

    # Persist minimal state
    state = load_state()
    state[case_id] = {
        "thread_id": thread_id,
        "original_subject": HUMAN_SUBJECT,
        "original_body": COMPLAINT_BODY,
        "forward_to": forward_to,
        "cc": cc,
        "status_by_recipient": {},
        "rounds": 0
    }
    save_state(state)

    # 3) Monitor replies in that same thread (your manual forwards/replies)
    print("Now you can manually forward/reply. I will watch this thread and draft next responses.")

    for _ in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL_SECONDS)

        # Fetch thread messages
        th = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        messages = th.get("messages", []) or []
        # Sort by time
        messages = sorted(messages, key=lambda m: int(m.get("internalDate", "0")))

        # Find newest inbound message that is not processed
        newest = messages[-1] if messages else None
        if not newest:
            continue

        if processed_label_id in (newest.get("labelIds") or []):
            continue

        payload = newest.get("payload", {})
        headers = payload.get("headers", []) or []
        subj = get_header(headers, "Subject") or subject
        inbound_text = decode_plain_text(payload).strip()

        if not inbound_text:
            add_label(service, newest["id"], processed_label_id)
            continue

        parsed = parse_forward_to(subj)
        intended = parsed["forward_to"][0] if parsed["forward_to"] and parsed["forward_to"][0] != "UNKNOWN" else "(unknown)"

        # Draft response (still only to self unless you change)
        st = load_state().get(case_id, {})
        original_body = st.get("original_body", COMPLAINT_BODY)

        draft = llm_draft_next(original_body, inbound_text, intended)

        draft_subject = draft.get("reply_subject", f"Re: {subj}")
        draft_body = draft.get("reply_body", "")

        # Send draft to self in same thread (so you can copy/paste to the real recipient)
        draft_note = f"""[DRAFT RESPONSE — REVIEW BEFORE SENDING]
Intended recipient (from subject): {intended}
Outcome: {draft.get('outcome')} (conf {draft.get('confidence')})

--- DRAFT START ---
{draft_body}
--- DRAFT END ---
"""

        send_thread_reply(
            service=service,
            thread_id=thread_id,
            to_addr=YOUR_EMAIL,
            subject=draft_subject,
            body=draft_note,
        )

        add_label(service, newest["id"], processed_label_id)
        print("Draft reply sent to self for review (thread updated).")

    print("Monitoring timed out. Re-run to continue.")


if __name__ == "__main__":
    main()
