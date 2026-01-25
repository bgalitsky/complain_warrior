# text_processing.py
# -*- coding: utf-8 -*-

import json
from configparser import RawConfigParser
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import os

from openai import OpenAI

DEFAULT_MODEL = "gpt-5"

config_path='config.ini'
config = RawConfigParser()
config.read(config_path)

# Facebook credentials
email = config.get('Facebook', 'email')
password = config.get('Facebook', 'password')

# API keys
gemini_api_key = config.get('Gemini', 'api_key')
os.environ["OPENAI_API_KEY"] = config.get('OpenAI', 'api_key')



@dataclass
class AgentDecision:
    """
    A single turn decision produced by GPT.
    """
    action: str  # continue_thread | abandon_thread | request_user_info | ask_user_opinion | confirm_compensation | escalate | spawn_mediator_agent
    confidence: float
    rationale: List[str]
    user_requests: List[str]
    drafts: List[Dict[str, str]]  # [{"kind": "email", "subject": "...", "body": "...", "to_hint": "airline_support"}]
    next_checks: List[str]  # e.g. "wait_for_reply", "check_portal", "ask_user_upload_receipts"
    thread_status: str  # open | waiting | resolved | abandoned | escalated


SYSTEM = """You are ComplaintWarriorBrain: an expert at customer-complaint strategy and drafting.

You are given:
1) ORIGINAL_COMPLAINT: the user's original complaint content (facts and demands).
2) THREAD: the current email thread transcript (all messages so far, newest at bottom).
3) USER_DATA: any new info the user provided at this step (receipts, amounts, deadlines, preferences).
4) CONTEXT: metadata like intended recipients, escalation history, previous actions, and "safe mode" policy.

Your job:
- Decide what to do next for this specific thread:
  * continue_thread: respond to the last inbound
  * abandon_thread: stop spending effort here (unproductive / wrong party / dead end)
  * request_user_info: ask user for missing documents or facts
  * ask_user_opinion: ask user to choose among options (accept partial offer? escalate?)
  * confirm_compensation: confirm resolution steps and close the thread
  * escalate: draft escalation message to regulator/consumer protection
  * spawn_mediator_agent: propose a new "agent" (e.g., mediator letter) and draft it

IMPORTANT:
- Every draft MUST include a `to_hint` string describing the agent/recipient category, such as:
  "airline_support", "regulator", "consumer_protection", "mediator", "executive_escalation".
- Drafts are written as emails (subject+body).
- Do not invent facts or claim attachments not provided.

Constraints:
- Be calm, factual, and firm. No threats.
- Respect SAFE MODE: drafts are sent only to the user themself for review.
- Return STRICT JSON matching the schema in the user prompt.
"""


SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "array", "items": {"type": "string"}},
        "user_requests": {"type": "array", "items": {"type": "string"}},
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "to_hint": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"}
                },
                "required": ["kind", "subject", "body"]
            }
        },
        "next_checks": {"type": "array", "items": {"type": "string"}},
        "thread_status": {"type": "string"}
    },
    "required": ["action", "confidence", "rationale", "user_requests", "drafts", "next_checks", "thread_status"]
}


class TextProcessing:
    """
    The 'brain': GPT decides next actions for a thread given complaint + thread transcript + user data.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL):
        self.client = OpenAI(api_key=api_key) if api_key else OpenAI()
        self.model = model

    def decide_next(
        self,
        original_complaint: str,
        thread_transcript: str,
        user_data: Dict[str, Any],
        context: Dict[str, Any],
        log_cb=None
    ) -> AgentDecision:
        if log_cb:
            log_cb("GPT-5 request issued: decide_next()")

        user_prompt = {
            "ORIGINAL_COMPLAINT": original_complaint,
            "THREAD": thread_transcript,
            "USER_DATA": user_data,
            "CONTEXT": context,
            "OUTPUT_SCHEMA": SCHEMA,
            "INSTRUCTIONS": "Return STRICT JSON only."
        }

        resp = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)}
            ],
            text={"format": {"type": "json_object"}}
        )
        data = json.loads(resp.output_text)

        return AgentDecision(
            action=data["action"],
            confidence=float(data["confidence"]),
            rationale=list(data.get("rationale", [])),
            user_requests=list(data.get("user_requests", [])),
            drafts=list(data.get("drafts", [])),
            next_checks=list(data.get("next_checks", [])),
            thread_status=data.get("thread_status", "open"),
        )

    def rewrite_complaint_professional(self, raw_text: str, log_cb=None) -> str:
        sys = """Rewrite the user's complaint into a professional, concise email.
Preserve facts, do not invent details. Use bullets for key facts and clear remedy request.
Return ONLY the body text (no JSON)."""
        if log_cb:
            log_cb("GPT-5 request issued: rewrite_complaint_professional()")

        resp = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": sys},
                {"role": "user", "content": raw_text}
            ],
        )
        return resp.output_text.strip()
