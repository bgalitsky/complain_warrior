from configparser import RawConfigParser
# text_processor_refactored.py
# -*- coding: utf-8 -*-
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any, Callable
import json
import os
import re

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
class ResolutionStrategy:
    primary_goal: str
    acceptable_fallbacks: List[str]
    escalate_if: List[str]
    evidence_needed: List[str]


@dataclass
class AgentDecision:
    action: str                     # draft_reply | ask_user_docs | wait | escalate | resolved
    confidence: float
    complaint_stage: str           # initial_demand | awaiting_company_response | negotiation | resolution_check | resolved | escalated
    rationale: str
    drafts: List[Dict[str, str]]
    debug_payload: Dict[str, Any]

def _maybe_openai_client():
    try:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        return OpenAI(api_key=api_key)
    except Exception:
        return None


@dataclass
class SatisfactionDecision:
    verdict: str                   # resolved | rejected | mixed_signals_needs_review
    reason: str
    signals: Dict[str, Any]
    gpt_used: bool = False


class TextProcessing:
    """
    Refactored decision engine:
    - clear complaint journey: initial demand -> negotiation -> resolution check
    - explicit resolution strategy
    - logs exact input/output payloads for traceability
    """

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("CW_GPT_MODEL", "gpt-4.1-mini")

    def _log(self, log_cb: Optional[Callable[[str], None]], msg: str):
        if log_cb:
            log_cb(msg)

    def _short(self, s: str, n: int = 600) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[:n] + " …"

    def rewrite_complaint_professional(self, complaint_raw: str, log_cb=None) -> str:
        raw = (complaint_raw or "").strip()
        if not raw:
            return ""
        out = (
            "Hello,\n\n"
            "I am contacting you regarding the following unresolved customer issue:\n\n"
            f"{raw}\n\n"
            "Please review the issue, confirm the appropriate remedy, and provide a timeline for resolution.\n\n"
            "Thank you."
        )
        self._log(log_cb, "[rewrite_complaint_professional] generated professional complaint text.")
        return out

    def extract_resolution_strategy(self, complaint_text: str, log_cb=None) -> ResolutionStrategy:
        t = (complaint_text or "").lower()

        goal = "general resolution"
        if "refund" in t or "reimburse" in t:
            goal = "refund/reimbursement"
        elif "replacement" in t:
            goal = "replacement"
        elif "rebook" in t:
            goal = "rebooking"
        elif "credit" in t or "voucher" in t:
            goal = "credit/voucher"

        fallbacks: List[str] = []
        if goal == "refund/reimbursement":
            fallbacks = ["credit/voucher", "rebooking", "goodwill compensation"]
        elif goal == "replacement":
            fallbacks = ["refund/reimbursement", "store credit"]
        elif goal == "rebooking":
            fallbacks = ["voucher", "refund/reimbursement"]
        else:
            fallbacks = ["refund/reimbursement", "credit/voucher"]

        escalate_if = [
            "explicit denial",
            "no reply after follow-up",
            "ambiguous partial offer that does not satisfy demand",
        ]

        evidence_needed = []
        if any(k in t for k in ["hotel", "meal", "receipt", "invoice", "expense"]):
            evidence_needed.extend(["receipts", "expense proof"])
        if "delay" in t or "missed connection" in t:
            evidence_needed.append("itinerary / ticket or booking reference")

        strat = ResolutionStrategy(
            primary_goal=goal,
            acceptable_fallbacks=fallbacks,
            escalate_if=escalate_if,
            evidence_needed=evidence_needed,
        )
        self._log(log_cb, f"[extract_resolution_strategy] {asdict(strat)}")
        return strat

    def decide_next(
        self,
        complaint_text: str,
        complaint_stage: str,
        current_status_summary: str,
        combined_transcript: str,
        strategy: ResolutionStrategy,
        user_data: Dict[str, Any],
        log_cb=None,
    ) -> AgentDecision:
        """
        Decide next operational step. This is intentionally transparent and rule-first.
        """
        transcript = (combined_transcript or "").strip()
        has_docs = bool(user_data.get("available_docs"))

        debug_payload = {
            "complaint_excerpt": self._short(complaint_text),
            "complaint_stage": complaint_stage,
            "status_summary": current_status_summary,
            "combined_transcript_excerpt": self._short(transcript),
            "strategy": asdict(strategy),
            "user_data": user_data,
        }
        self._log(log_cb, "[decide_next] INPUT " + json.dumps(debug_payload, ensure_ascii=False))

        lower = transcript.lower()

        drafts: List[Dict[str, str]] = []
        action = "wait"
        next_stage = complaint_stage
        rationale = "No action required yet."
        confidence = 0.60

        if complaint_stage == "initial_demand":
            action = "draft_reply"
            next_stage = "awaiting_company_response"
            confidence = 0.86
            rationale = "Initial demand should be sent first."
            drafts = [{
                "to_hint": "company_support",
                "subject": "Formal complaint and requested remedy",
                "body": complaint_text
            }]

        elif any(k in lower for k in ["please provide", "please send", "documentation", "receipt", "invoice", "proof"]):
            if has_docs:
                action = "draft_reply"
                next_stage = "negotiation"
                confidence = 0.80
                rationale = "Company asked for supporting documents and user has uploaded them."
                docs = ", ".join(user_data.get("available_docs", [])[:8])
                drafts = [{
                    "to_hint": "company_support",
                    "subject": "Requested supporting documents",
                    "body": (
                        "Hello,\n\n"
                        f"Please find the requested supporting materials attached ({docs}). "
                        "Please confirm receipt and proceed with resolving the complaint.\n\nRegards"
                    ),
                }]
            else:
                action = "ask_user_docs"
                next_stage = "negotiation"
                confidence = 0.83
                rationale = "Company requested documents; user must attach them."
                drafts = [{
                    "to_hint": "internal_user",
                    "subject": "Documents requested",
                    "body": "The company requested receipts or supporting documents. Please upload them so the system can continue."
                }]

        elif any(k in lower for k in ["we can offer", "we can provide", "voucher", "credit", "refund", "reimburse", "approved", "processed"]):
            action = "draft_reply"
            next_stage = "resolution_check"
            confidence = 0.76
            rationale = "Company appears to be offering a remedy; seek concrete confirmation and timeline."
            drafts = [{
                "to_hint": "company_support",
                "subject": "Please confirm resolution details",
                "body": (
                    "Hello,\n\nThank you for your reply. "
                    "Please confirm the exact remedy being provided, including amount/value and when it will be issued.\n\nRegards"
                )
            }]

        elif any(k in lower for k in ["cannot", "unable", "not eligible", "denied", "no compensation", "we will not"]):
            action = "escalate"
            next_stage = "escalated"
            confidence = 0.74
            rationale = "Company appears to deny the requested resolution."
            drafts = [{
                "to_hint": "mediator_or_regulator",
                "subject": "Escalation request for unresolved complaint",
                "body": (
                    "Hello,\n\nI am escalating an unresolved complaint because the company appears to have denied the requested resolution. "
                    "Please review the case and advise next steps.\n\nRegards"
                )
            }]

        else:
            action = "draft_reply"
            next_stage = "negotiation"
            confidence = 0.66
            rationale = "Continue negotiation with a clarifying follow-up."
            drafts = [{
                "to_hint": "company_support",
                "subject": "Follow-up on complaint resolution",
                "body": (
                    "Hello,\n\nFollowing up on my complaint. Please clarify the next steps and expected timeline for resolving it.\n\nRegards"
                )
            }]

        dec = AgentDecision(
            action=action,
            confidence=confidence,
            complaint_stage=next_stage,
            rationale=rationale,
            drafts=drafts,
            debug_payload=debug_payload,
        )
        self._log(log_cb, "[decide_next] OUTPUT " + json.dumps(asdict(dec), ensure_ascii=False))
        return dec

    def detect_satisfaction_rules(self, inbound_text: str, strategy: ResolutionStrategy, log_cb=None) -> SatisfactionDecision:
        t = (inbound_text or "").lower()

        strong_positive = [
            "refund has been issued", "refund will be issued", "we will refund",
            "reimbursement has been approved", "we will reimburse", "reimbursement approved",
            "replacement has been shipped", "we will replace",
            "we have rebooked", "we will rebook",
            "credit has been applied", "voucher will be sent", "we can provide you with the voucher",
        ]
        strong_negative = [
            "cannot", "unable to", "not eligible", "denied", "we will not", "no compensation", "no refund",
        ]
        req_more = [
            "please provide", "please send", "documentation", "receipt", "invoice", "proof",
        ]

        pos_hits = [p for p in strong_positive if p in t]
        neg_hits = [n for n in strong_negative if n in t]
        req_hits = [r for r in req_more if r in t]

        # primary-goal-aware handling:
        if strategy.primary_goal == "refund/reimbursement":
            # voucher/credit only is not necessarily satisfactory for a refund demand
            voucher_only = ("voucher" in t or "credit" in t) and not any(k in t for k in ["refund", "reimburse"])
            if voucher_only:
                return SatisfactionDecision(
                    verdict="mixed_signals_needs_review",
                    reason="Company offered voucher/credit while primary goal is refund/reimbursement.",
                    signals={"pos_hits": pos_hits, "neg_hits": neg_hits, "req_hits": req_hits, "goal": strategy.primary_goal},
                )

        if pos_hits and not neg_hits:
            return SatisfactionDecision(
                verdict="resolved",
                reason=f"Detected concrete agreement language: {pos_hits[:3]}",
                signals={"pos_hits": pos_hits, "neg_hits": neg_hits, "req_hits": req_hits, "goal": strategy.primary_goal},
            )

        if neg_hits and not pos_hits:
            return SatisfactionDecision(
                verdict="rejected",
                reason=f"Detected denial language: {neg_hits[:3]}",
                signals={"pos_hits": pos_hits, "neg_hits": neg_hits, "req_hits": req_hits, "goal": strategy.primary_goal},
            )

        if req_hits:
            return SatisfactionDecision(
                verdict="mixed_signals_needs_review",
                reason="Company is requesting more information/documents.",
                signals={"pos_hits": pos_hits, "neg_hits": neg_hits, "req_hits": req_hits, "goal": strategy.primary_goal},
            )

        return SatisfactionDecision(
            verdict="mixed_signals_needs_review",
            reason="No clear, concrete satisfaction or rejection signal found.",
            signals={"pos_hits": pos_hits, "neg_hits": neg_hits, "req_hits": req_hits, "goal": strategy.primary_goal},
        )

    def detect_satisfaction_gpt(self, inbound_text: str, strategy: ResolutionStrategy,
                                log_cb=None) -> SatisfactionDecision:
        client = _maybe_openai_client()
        if not client:
            return SatisfactionDecision(
                verdict="mixed_signals_needs_review",
                reason="GPT fallback unavailable (OPENAI_API_KEY not set).",
                signals={"goal": strategy.primary_goal},
                gpt_used=False,
            )

        prompt = f"""
    You are deciding whether a company response should TERMINATE a complaint-resolution loop.

    Primary requested outcome: {strategy.primary_goal}
    Acceptable fallback outcomes: {strategy.acceptable_fallbacks}

    Company response:
    {inbound_text}

    Return JSON only with:
    - verdict: one of ["resolved","rejected","mixed_signals_needs_review"]
    - reason: short explanation
    - key_phrases: list of exact phrases from company response supporting the decision

    Decision rules:
    1. If the company is clearly cooperating and offers a concrete monetary compensation,
       refund, reimbursement, replacement, rebooking, voucher, or credit, usually return "resolved".
    2. If the company offers compensation with a specific amount or concrete commitment
       (for example: "I will compensate you $100"), return "resolved".
    3. If the company asks for more documents, delays, or makes an unclear/conditional statement,
       return "mixed_signals_needs_review".
    4. If the company clearly refuses or denies the request, return "rejected".
    5. Be practical: if the company is cooperating and proposing a real remedy, accept it and terminate the process.
    """

        self._log(log_cb, "[detect_satisfaction_gpt] INPUT " + json.dumps({
            "primary_goal": strategy.primary_goal,
            "acceptable_fallbacks": strategy.acceptable_fallbacks,
            "response_excerpt": self._short(inbound_text),
        }, ensure_ascii=False))

        try:
            resp = client.responses.create(
                model=self.model,
                input=prompt,
                temperature=0,
            )

            txt = (getattr(resp, "output_text", None) or "").strip()

            # If output_text is empty, try to reconstruct text from output items
            if not txt and getattr(resp, "output", None):
                parts = []
                for item in resp.output:
                    content = getattr(item, "content", None) or []
                    for c in content:
                        ctext = getattr(c, "text", None)
                        if ctext:
                            parts.append(ctext)
                txt = "\n".join(parts).strip()

            self._log(log_cb, "[detect_satisfaction_gpt] RAW " + (txt[:2000] if txt else "<EMPTY>"))

            # strip markdown fences if model wrapped JSON
            if txt.startswith("```"):
                txt = txt.strip("`")
                txt = txt.replace("json\n", "", 1).strip()

            # try direct JSON first
            try:
                data = json.loads(txt)
            except Exception:
                # try extracting first {...} block
                m = re.search(r"\{.*\}", txt, re.S)
                if not m:
                    raise ValueError(f"No JSON object found in model output: {txt[:500]}")
                data = json.loads(m.group(0))

            out = SatisfactionDecision(
                verdict=data.get("verdict", "mixed_signals_needs_review"),
                reason=data.get("reason", "GPT satisfaction decision."),
                signals={
                    "key_phrases": data.get("key_phrases", []),
                    "primary_goal": strategy.primary_goal,
                    "acceptable_fallbacks": strategy.acceptable_fallbacks,
                    "response_excerpt": self._short(inbound_text, 1000),
                    "raw_model_output": txt[:2000],
                },
                gpt_used=True,
            )
            self._log(log_cb, "[detect_satisfaction_gpt] OUTPUT " + json.dumps(asdict(out), ensure_ascii=False))
            return out

        except Exception as e:
            out = SatisfactionDecision(
                verdict="mixed_signals_needs_review",
                reason=f"GPT fallback failed: {e}",
                signals={"primary_goal": strategy.primary_goal},
                gpt_used=True,
            )
            self._log(log_cb, "[detect_satisfaction_gpt] ERROR " + json.dumps(asdict(out), ensure_ascii=False))
            return out

    def detect_satisfaction_with_fallback(
            self,
            inbound_text: str,
            strategy: ResolutionStrategy,
            trusted: bool,
            log_cb=None,
    ) -> SatisfactionDecision:
        """
        GPT-first satisfaction decision.

        Behavior:
        - If trusted=True and OpenAI is available -> use GPT directly.
        - If trusted=False -> stay conservative and return mixed_signals_needs_review
          unless you want to call GPT in manual mode too.
        - If GPT is unavailable/fails -> return mixed_signals_needs_review.
        """
        self._log(
            log_cb,
            "[detect_satisfaction_with_fallback] INPUT " + json.dumps({
                "trusted": trusted,
                "primary_goal": strategy.primary_goal,
                "acceptable_fallbacks": strategy.acceptable_fallbacks,
                "response_excerpt": self._short(inbound_text),
            }, ensure_ascii=False)
        )
        """ 
        if not trusted:
            out = SatisfactionDecision(
                verdict="mixed_signals_needs_review",
                reason="Trusted mode is OFF, so GPT satisfaction resolution is not applied automatically.",
                signals={
                    "primary_goal": strategy.primary_goal,
                    "acceptable_fallbacks": strategy.acceptable_fallbacks,
                },
                gpt_used=False,
            )
            self._log(log_cb,
                      "[detect_satisfaction_with_fallback] OUTPUT " + json.dumps(asdict(out), ensure_ascii=False))
            return out
        """
        gpt = self.detect_satisfaction_gpt(inbound_text, strategy, log_cb=log_cb)
        self._log(log_cb, "[detect_satisfaction_with_fallback] OUTPUT " + json.dumps(asdict(gpt), ensure_ascii=False))
        return gpt
