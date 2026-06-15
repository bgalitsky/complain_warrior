# ngrok http 5000

import json
import time
import re
import configparser
from typing import List, Union, Dict, Any, Optional
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from twilio.rest import Client as TwilioClient
from openai import OpenAI


class ComplaintCallAgent:
    """
    High-level callable from Gmail Complaint Warrior.

    This will:
      - extract a phone number directly from the complaint if one is present
      - use GPT to identify the vendor / business and discover likely support numbers
      - generate a spoken phone script from the initial complaint and current resolution state
      - place call(s) via Twilio
      - wait for inbound (agent) transcription
      - return agent reply text (string)
    """

    def __init__(self, config_path: str = "config.ini"):
        self.cfg = configparser.ConfigParser(strict=False)
        ok = self.cfg.read(config_path, encoding="utf-8")
        if not ok:
            raise RuntimeError(f"Could not read config file: {config_path}")

        # Server base (must be same origin as your Flask webhook server)
        self.base = self.cfg["server"]["voice_webhook_base"].strip().rstrip("/")

        # Twilio
        self.account_sid = self.cfg["twilio"]["account_sid"].strip()
        self.auth_token = self.cfg["twilio"]["auth_token"].strip()
        self.from_number = self.cfg["twilio"]["from_number"].strip()
        self.amd_mode = self.cfg.get("behavior", "amd_mode", fallback="DetectMessageEnd").strip()
        self.twilio = TwilioClient(self.account_sid, self.auth_token)

        # OpenAI (GPT)
        self.openai_key = self.cfg["OpenAI"]["api_key"].strip()
        self.openai_model = self.cfg["OpenAI"].get("model", "gpt-4.1-mini").strip()
        self.oa = OpenAI(api_key=self.openai_key)

        # Optional directory of trusted vendor phone numbers
        self.vendor_dir_path = self.cfg.get("phone_router", "vendor_directory_json", fallback="").strip()
        self.vendor_dir = self._load_vendor_dir(self.vendor_dir_path)

    # -------------------- vendor directory --------------------

    def _load_vendor_dir(self, path: str) -> Dict[str, List[Dict[str, Any]]]:
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _dir_lookup(self, vendor: str) -> List[Dict[str, Any]]:
        vendor = (vendor or "").strip()
        if not vendor:
            return []
        if vendor in self.vendor_dir:
            return self.vendor_dir[vendor]
        for k, v in self.vendor_dir.items():
            if k.lower() == vendor.lower():
                return v
        return []

    # -------------------- extraction helpers --------------------

    @staticmethod
    def _clean_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _extract_phone_candidates(text: str) -> List[str]:
        text = text or ""
        # Accept common US/international forms, normalize to E.164 when possible.
        pattern = re.compile(r"(?<!\w)(\+?\d[\d\s().\-]{7,}\d)")
        out: List[str] = []
        seen = set()
        for raw in pattern.findall(text):
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 10:
                normalized = "+1" + digits
            elif len(digits) == 11 and digits.startswith("1"):
                normalized = "+" + digits
            elif len(digits) >= 11 and raw.strip().startswith("+"):
                normalized = "+" + digits
            else:
                continue
            if normalized not in seen:
                out.append(normalized)
                seen.add(normalized)
        return out

    @staticmethod
    def _extract_business_name_hint(text: str) -> str:
        text = text or ""
        patterns = [
            r"(?:business|company|merchant|vendor|store|hotel|airline|restaurant)\s*(?:name)?\s*[:\-]\s*([^\n,.;]{2,80})",
            r"(?:at|from|with)\s+([A-Z][A-Za-z0-9&'.,\- ]{2,80}?)(?=(?:\s+located\s+at|\s+at\s+\d|\s*,\s*\d|[\n.;]|$))",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return ComplaintCallAgent._clean_whitespace(m.group(1).strip(" ,.-"))
        return ""

    @staticmethod
    def _extract_business_address_hint(text: str) -> str:
        text = text or ""
        patterns = [
            r"(?:address|located at|business address)\s*[:\-]?\s*([^\n]{8,160})",
            r"(\d{1,6}\s+[A-Za-z0-9.#'\- ]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Suite|Ste)\b[^\n,.;]{0,120})",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return ComplaintCallAgent._clean_whitespace(m.group(1).strip(" ,.-"))
        return ""

    def _safe_json_loads(self, raw: str) -> Dict[str, Any]:
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("Empty model output")
        try:
            return json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                raise
            return json.loads(m.group(0))

    # -------------------- GPT: reformulate + discover numbers --------------------

    def reformulate_for_call(
        self,
        user_complaint: str,
        vendor_hint: Optional[str] = None,
        complaint_stage: Optional[str] = None,
        current_status_summary: Optional[str] = None,
        business_name_hint: Optional[str] = None,
        business_address_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns:
          {
            "vendor": str,
            "business_name": str,
            "business_address": str,
            "call_script": str,
            "to_numbers": [{number,is_ivr,ivr_digits,ivr_wait,label,confidence,source}, ...],
            "needs_verification": bool,
            "notes": str
          }
        """
        user_complaint = (user_complaint or "").strip()
        complaint_stage = (complaint_stage or "").strip()
        current_status_summary = (current_status_summary or "").strip()

        if not user_complaint:
            return {
                "vendor": vendor_hint or "",
                "business_name": business_name_hint or "",
                "business_address": business_address_hint or "",
                "call_script": "Hello. I have a customer complaint and I need help resolving it.",
                "to_numbers": [],
                "needs_verification": True,
                "notes": "Empty complaint.",
            }

        direct_numbers = self._extract_phone_candidates(user_complaint)
        business_name_hint = (business_name_hint or self._extract_business_name_hint(user_complaint) or vendor_hint or "").strip()
        business_address_hint = (business_address_hint or self._extract_business_address_hint(user_complaint) or "").strip()

        system = (
            "You prepare automated call from customer to customer support or company representative. "
            "Return ONLY valid JSON matching the schema.\n\n"
            "Task:\n"
            "1) Build a short spoken call  script (of a customer to the company, max 6 sentences) from the initial complaint and the current resolution state.\n"
            "2) Identify the most likely business/vendor name.\n"
            "3) Infer or confirm the business address if possible.\n"
            "4) Provide the best support/customer-service phone numbers in E.164 format (+1...).\n"
            "5) If a phone number already appears in the complaint, include it with high confidence and source='complaint_text'.\n"
            "6) If the business name and/or address is given, use that to choose the most plausible support number.\n"
            "7) If you know IVR navigation, provide digits using 'w' for pauses (e.g., 'wwww0ww1').\n"
            "8) If uncertain, set needs_verification=true.\n\n"
            "Schema:\n"
            "{\n"
            '  "vendor": "string",\n'
            '  "business_name": "string",\n'
            '  "business_address": "string",\n'
            '  "call_script": "string",\n'
            '  "to_numbers": [\n'
            "    {\n"
            '      "number": "string",\n'
            '      "is_ivr": boolean,\n'
            '      "ivr_digits": "string",\n'
            '      "ivr_wait": integer,\n'
            '      "label": "string",\n'
            '      "confidence": number,\n'
            '      "source": "string"\n'
            "    }\n"
            "  ],\n"
            '  "needs_verification": boolean,\n'
            '  "notes": "string"\n'
            "}\n"
        )

        user_payload = {
            "vendor_hint": vendor_hint or "",
            "business_name_hint": business_name_hint,
            "business_address_hint": business_address_hint,
            "complaint_stage": complaint_stage,
            "current_status_summary": current_status_summary,
            "complaint": user_complaint,
            "direct_numbers_found_in_complaint": direct_numbers,
        }

        try:
            resp = self.oa.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                temperature=0.2,
            )
            raw = (resp.choices[0].message.content or "").strip()
            data = self._safe_json_loads(raw)
        except Exception as e:
            data = {
                "vendor": vendor_hint or business_name_hint or "",
                "business_name": business_name_hint or vendor_hint or "",
                "business_address": business_address_hint or "",
                "call_script": user_complaint[:800],
                "to_numbers": [],
                "needs_verification": True,
                "notes": f"GPT did not return valid JSON: {e}",
            }

        vendor = (data.get("vendor") or vendor_hint or business_name_hint or "").strip()
        business_name = (data.get("business_name") or business_name_hint or vendor or "").strip()
        business_address = (data.get("business_address") or business_address_hint or "").strip()
        data["vendor"] = vendor
        data["business_name"] = business_name
        data["business_address"] = business_address

        # Merge sources in priority order: complaint text -> trusted directory -> GPT discovered numbers
        merged: List[Dict[str, Any]] = []
        seen = set()

        def add_number(number: str, *, source: str, label: str, confidence: float, is_ivr: bool = False,
                       ivr_digits: str = "", ivr_wait: int = 10):
            n = (number or "").strip()
            if not n or n in seen:
                return
            merged.append({
                "number": n,
                "is_ivr": bool(is_ivr),
                "ivr_digits": str(ivr_digits or "").strip(),
                "ivr_wait": int(ivr_wait or 10),
                "label": str(label or "").strip(),
                "confidence": float(confidence or 0.5),
                "source": source,
            })
            seen.add(n)

        for n in direct_numbers:
            add_number(n, source="complaint_text", label="number from complaint", confidence=0.99)

        directory = self._dir_lookup(vendor or business_name)
        for d in directory:
            add_number(
                str(d.get("number", "")),
                source="vendor_directory",
                label=str(d.get("label", "trusted directory") or "trusted directory"),
                confidence=float(d.get("confidence", 0.98) or 0.98),
                is_ivr=bool(d.get("is_ivr", False)),
                ivr_digits=str(d.get("ivr_digits", "") or ""),
                ivr_wait=int(d.get("ivr_wait", 10) or 10),
            )

        for g in (data.get("to_numbers") or []):
            add_number(
                str(g.get("number", "")),
                source=str(g.get("source", "gpt") or "gpt"),
                label=str(g.get("label", "") or ""),
                confidence=float(g.get("confidence", 0.5) or 0.5),
                is_ivr=bool(g.get("is_ivr", False)),
                ivr_digits=str(g.get("ivr_digits", "") or ""),
                ivr_wait=int(g.get("ivr_wait", 10) or 10),
            )

        data["to_numbers"] = merged
        data["call_script"] = self._clean_whitespace(data.get("call_script") or "")
        data.setdefault("needs_verification", True)
        data.setdefault("notes", "")

        # Complaint number / directory entries count as more trusted than GPT-only discovery.
        if direct_numbers or directory:
            data["needs_verification"] = False

        if not data["call_script"]:
            data["call_script"] = (
                "Hello. I am calling about an unresolved customer complaint. "
                "Please review the case and help me resolve it today."
            )

        return data

    # -------------------- Twilio call placement --------------------

    def make_calls(self, to_numbers: List[Union[str, Dict[str, Any]]], call_script: str) -> List[Dict[str, str]]:
        results = []
        for entry in to_numbers:
            if isinstance(entry, str):
                number = entry
                ivr_digits, ivr_wait, is_ivr = "", 10, False
            else:
                number = entry["number"]
                ivr_digits = entry.get("ivr_digits", "")
                ivr_wait = int(entry.get("ivr_wait", 10))
                is_ivr = bool(entry.get("is_ivr", False))

            params = {"complaint": call_script}
            if ivr_digits:
                params["ivr_digits"] = ivr_digits
                params["ivr_wait"] = str(ivr_wait)
            if is_ivr:
                params["is_ivr"] = "1"

            qs = urlencode(params)
            voice_url = f"{self.base}/voice_human?{qs}"
            status_url = f"{self.base}/status?{qs}"

            call = self.twilio.calls.create(
                to=number,
                from_=self.from_number,
                url=voice_url,
                method="POST",
                machine_detection=self.amd_mode,
                async_amd=True,
                status_callback=status_url,
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )

            results.append({"to": number, "call_sid": call.sid})
        return results

    # -------------------- transcript retrieval --------------------

    def _fetch_result(self, call_sid: str) -> Dict[str, Any]:
        url = f"{self.base}/result/{call_sid}"
        with urlopen(url, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="replace").strip()

        if not raw:
            return {"transcript": "", "status": "empty_result", "call_sid": call_sid}

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {
                "transcript": "",
                "status": "non_json_result",
                "call_sid": call_sid,
                "raw_preview": raw[:500],
            }

    def wait_for_transcript(self, call_sid: str, overall_timeout: int = 300, idle_after_inbound: int = 6) -> str:
        """
        Wait until inbound_track appears (agent spoke), then wait for transcript to stabilize.
        """
        deadline = time.time() + overall_timeout
        inbound_seen = False

        last = ""
        while time.time() < deadline:
            try:
                data = self._fetch_result(call_sid)
                tr = data.get("transcript", "") or ""
            except (HTTPError, URLError, TimeoutError,  json.JSONDecodeError, ValueError):
                time.sleep(1)
                continue

            if "[inbound_track]" in tr:
                inbound_seen = True

            if not inbound_seen:
                time.sleep(1)
                continue

            time.sleep(idle_after_inbound)
            try:
                data2 = self._fetch_result(call_sid)
                tr2 = data2.get("transcript", "") or ""
            except (HTTPError, URLError, TimeoutError):
                return tr

            if tr2 == tr:
                return tr2

            last = tr2
            time.sleep(1)

        try:
            return self._fetch_result(call_sid).get("transcript", "") or ""
        except Exception:
            return last

    @staticmethod
    def extract_agent_only(full_transcript: str) -> str:
        lines = []
        for line in (full_transcript or "").splitlines():
            if line.startswith("[inbound_track]"):
                lines.append(line.replace("[inbound_track]", "").strip())
        return "\n".join(lines).strip()

    # -------------------- one-shot API for Complaint Warrior --------------------

    def call_and_get_reply_autoroute(
            self,
            user_complaint: str,
            vendor_hint: Optional[str] = None,
            timeout: int = 300,
            complaint_stage: Optional[str] = None,
            current_status_summary: Optional[str] = None,
    ) -> str:
        """
        Main entry:
          - build script + numbers from complaint/current state
          - place call
          - return agent reply
        """
        plan = self.reformulate_for_call(
            user_complaint=user_complaint,
            vendor_hint=vendor_hint,
            complaint_stage=complaint_stage,
            current_status_summary=current_status_summary,
        )

        call_script = (plan.get("call_script") or "").strip()
        to_numbers = plan.get("to_numbers") or []

        if not call_script:
            src = (user_complaint or "").strip()
            if src:
                call_script = src[:800]
            else:
                call_script = "Hello. I have a customer complaint and I need help resolving it."

        if not to_numbers:
            return ""

        print("DEBUG call_script:", repr(call_script))
        print("DEBUG to_numbers:", to_numbers)
        print("DEBUG plan:", json.dumps(plan, ensure_ascii=False, indent=2))

        results = self.make_calls(to_numbers, call_script)
        if not results:
            return ""

        sid = results[0]["call_sid"]
        full = self.wait_for_transcript(sid, overall_timeout=timeout)
        return self.extract_agent_only(full)


if __name__ == "__main__":
    agent = ComplaintCallAgent("config.ini")
    reply = agent.call_and_get_reply_autoroute(
        "I missed my flight connection due to a delay on the first leg. I want reimbursement and compensation. The airline is Southwest.",
        vendor_hint="Southwest",
        complaint_stage="negotiation",
        current_status_summary="The airline has not yet offered reimbursement for hotel and meal expenses.",
        timeout=300,
    )
    print("\nAGENT REPLY:\n", reply)
