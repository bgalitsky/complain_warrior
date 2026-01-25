import os
import time
import json
import base64
import re
from configparser import RawConfigParser
from dataclasses import dataclass
from typing import Optional, Tuple

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

# Use a test address first. Replace with real support address only if you intend to actually send.
SUPPORT_EMAIL = "bgalitsky@hotmail.com"

YOUR_NAME = "Boris Galitsky"
YOUR_EMAIL = "bgalitsky@hotmail.com"

# How long to wait (polling) for a response
POLL_INTERVAL_SECONDS = 60
MAX_POLLS = 60  # 60 minutes

# Label to avoid replying multiple times
PROCESSED_LABEL = "AUTO_LLM_REPLIED"

# OpenAI model (user requested GPT-5)
OPENAI_MODEL = "gpt-5"

client = OpenAI()

# -----------------------------
# SAMPLE INITIAL COMPLAINT
# -----------------------------
COMPLAINT_SUBJECT = "Compensation Request – Delayed Flight WN4587 (June 12)"
COMPLAINT_BODY = f"""Dear Customer Relations,

I am writing to file a formal complaint regarding Flight WN4587 from Denver to San Jose on June 12, 2026.

The flight was delayed for over 4 hours due to what was described at the gate as “operational issues.” As a result, I missed a prepaid hotel reservation and an important business meeting. No meal vouchers or hotel assistance were offered at the airport.

Given the inconvenience and direct financial impact, I am requesting:
1) Reimbursement for my hotel cancellation fee ($180)
2) A reasonable travel credit for the disruption

Please let me know what documentation you require to process this request.

Sincerely,
{YOUR_NAME}
{YOUR_EMAIL}
Reservation: Q7K2LM
"""

# -----------------------------
# GMAIL AUTH + HELPERS
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
            # For headless servers, use: creds = flow.run_console()
            creds = flow.run_local_server(port=0)
        with open(TOKEN_JSON, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def get_header(headers, name):
    for h in headers:
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
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


def add_label(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def has_label(message, label_id: str) -> bool:
    return label_id in (message.get("labelIds") or [])


def decode_plain_text(payload) -> str:
    """Extract text/plain from Gmail message payload (best-effort)."""
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

    # fallback: try any text/*
    for part in payload.get("parts", []) or []:
        if (part.get("mimeType") or "").startswith("text/"):
            pdata = (part.get("body") or {}).get("data")
            if pdata:
                return base64.urlsafe_b64decode(pdata.encode("utf-8")).decode("utf-8", errors="replace")

    return ""


def send_new_email(service, to_email: str, subject: str, body_text: str):
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["To"] = to_email
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

    return service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": thread_id},
    ).execute()


# -----------------------------
# LLM: rejection detection + reply drafting
# -----------------------------
LLM_SYSTEM = """You are an assistant that helps a customer respond to customer-support emails.
You will be given:
(1) The customer's original complaint email text.
(2) The support agent's latest reply email text.

Your job:
A) Decide whether the support reply is a rejection/denial of the complaint (full or partial).
B) If it is a rejection, draft a concise, professional disagreement reply that:
   - remains factual and calm
   - asks for specific policy basis and re-evaluation
   - requests next steps and evidence requirements
   - avoids threats, profanity, or legal intimidation
C) If it's not a rejection (e.g., asking for more info), draft an appropriate cooperative reply.

Return STRICT JSON only with:
- is_rejection: boolean
- category: one of ["denial","partial_denial","needs_more_info","acceptance","generic","other"]
- confidence: number 0..1
- draft_reply_subject: string
- draft_reply_body: string
"""

def llm_classify_and_draft(original_complaint: str, support_reply: str) -> dict:
    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": f"ORIGINAL COMPLAINT:\n{original_complaint}\n\nSUPPORT REPLY:\n{support_reply}"}
        ],
        # Ask for JSON object output (Structured Outputs / JSON mode)
        text={"format": {"type": "json_object"}},
    )
    return json.loads(resp.output_text)


# -----------------------------
# Polling: find latest reply from support and respond once
# -----------------------------
def find_latest_support_message(service, from_email: str, newer_than_days: int = 14):
    query = f'from:{from_email} newer_than:{newer_than_days}d'
    results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    msgs = results.get("messages", [])
    if not msgs:
        return None
    # Fetch the newest message (first result)
    msg = service.users().messages().get(userId="me", id=msgs[0]["id"], format="full").execute()
    return msg


def extract_thread_original_sent_message(service, thread_id: str) -> str:
    """Best-effort: return earliest SENT message body in thread as original complaint."""
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = thread.get("messages", []) or []
    # old -> new
    messages_sorted = sorted(messages, key=lambda m: int(m.get("internalDate", "0")))
    for m in messages_sorted:
        if "SENT" in (m.get("labelIds") or []):
            return decode_plain_text(m.get("payload"))
    return ""


def main():
    # 1) Gmail service
    gmail_creds = get_gmail_creds()
    service = build("gmail", "v1", credentials=gmail_creds)
    processed_label_id = ensure_label(service, PROCESSED_LABEL)

    # 2) Send initial complaint (to support)
    print(f"Sending initial complaint to {SUPPORT_EMAIL} ...")
    sent = send_new_email(service, SUPPORT_EMAIL, COMPLAINT_SUBJECT, COMPLAINT_BODY)
    print("Sent complaint message id:", sent.get("id"))

    # 3) Poll for response
    print("Polling for a support reply...")
    for i in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL_SECONDS)

        msg = find_latest_support_message(service, SUPPORT_EMAIL, newer_than_days=14)
        if not msg:
            continue

        # Avoid double-processing
        if has_label(msg, processed_label_id):
            continue

        thread_id = msg.get("threadId")
        payload = msg.get("payload", {})
        headers = payload.get("headers", []) or []
        subject = get_header(headers, "Subject") or "(no subject)"
        from_addr = get_header(headers, "From") or SUPPORT_EMAIL
        message_id = get_header(headers, "Message-ID")
        references = get_header(headers, "References")

        support_text = decode_plain_text(payload).strip()
        if not support_text:
            continue

        original = extract_thread_original_sent_message(service, thread_id).strip()
        if not original:
            # fallback to our known complaint
            original = COMPLAINT_BODY

        # 4) LLM classification + draft
        decision = llm_classify_and_draft(original, support_text)
        print("LLM decision:", decision.get("category"), decision.get("confidence"))

        draft_subject = decision.get("draft_reply_subject") or f"Re: {subject}"
        draft_body = decision.get("draft_reply_body") or ""

        # 5) Send reply in-thread
        print("Sending LLM-written reply...")
        res = send_thread_reply(
            service=service,
            thread_id=thread_id,
            to_addr=from_addr,
            subject=draft_subject,
            body=draft_body,
            in_reply_to=message_id,
            references=references,
        )
        print("Sent reply id:", res.get("id"))

        # 6) Label processed
        add_label(service, msg["id"], processed_label_id)
        print("Done. Exiting.")
        return

    print("No reply detected within polling window.")


if __name__ == "__main__":
    main()
