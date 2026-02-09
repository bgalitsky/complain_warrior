# call.py
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
    High-level callable from Gmail Complaint Warrior:

      reply = ComplaintCallAgent("config.ini").call_and_get_reply_autoroute(user_complaint)

    This will:
      - use GPT to rewrite a phone script and propose support numbers (and vendor)
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
        self.auth_token  = self.cfg["twilio"]["auth_token"].strip()
        self.from_number = self.cfg["twilio"]["from_number"].strip()
        self.amd_mode    = self.cfg.get("behavior", "amd_mode", fallback="DetectMessageEnd").strip()
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

    # -------------------- GPT: reformulate + discover numbers --------------------

    def reformulate_for_call(self, user_complaint: str, vendor_hint: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns:
          {
            "vendor": str,
            "call_script": str,
            "to_numbers": [{number,is_ivr,ivr_digits,ivr_wait,label,confidence}, ...],
            "needs_verification": bool,
            "notes": str
          }
        """
        user_complaint = (user_complaint or "").strip()
        if not user_complaint:
            return {
                "vendor": vendor_hint or "",
                "call_script": "Hello. I have a customer complaint and I need help resolving it.",
                "to_numbers": [],
                "needs_verification": True,
                "notes": "Empty complaint."
            }

        system = (
            "You prepare automated customer-support phone calls.\n"
            "Return ONLY valid JSON matching the schema.\n\n"
            "Goal:\n"
            "1) Rewrite the complaint into a short spoken script (max ~6 sentences), polite but firm.\n"
            "2) Identify the most likely vendor/company.\n"
            "3) Provide likely US customer support phone numbers in E.164 format (+1...).\n"
            "4) If you know IVR navigation, provide digits using 'w' for pauses (e.g., 'wwww0ww1').\n"
            "If unsure about phone numbers, set needs_verification=true.\n\n"
            "Schema:\n"
            "{\n"
            '  "vendor": "string",\n'
            '  "call_script": "string",\n'
            '  "to_numbers": [\n'
            "    {\n"
            '      "number": "string",\n'
            '      "is_ivr": boolean,\n'
            '      "ivr_digits": "string",\n'
            '      "ivr_wait": integer,\n'
            '      "label": "string",\n'
            '      "confidence": number\n'
            "    }\n"
            "  ],\n"
            '  "needs_verification": boolean,\n'
            '  "notes": "string"\n'
            "}\n"
        )

        user_payload = {"vendor_hint": vendor_hint or "", "complaint": user_complaint}

        resp = self.oa.chat.completions.create(
            model=self.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
            ],
            temperature=0.2
        )

        raw = (resp.choices[0].message.content or "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            data = {
                "vendor": vendor_hint or "",
                "call_script": user_complaint[:800],
                "to_numbers": [],
                "needs_verification": True,
                "notes": "GPT did not return valid JSON."
            }

        vendor = (data.get("vendor") or vendor_hint or "").strip()
        data["vendor"] = vendor

        # Merge with directory (prefer directory first)
        directory = self._dir_lookup(vendor)
        merged = []
        seen = set()

        def norm_num(n: str) -> str:
            return (n or "").strip()

        for d in directory:
            n = norm_num(d.get("number"))
            if n and n not in seen:
                merged.append(d)
                seen.add(n)

        for g in (data.get("to_numbers") or []):
            n = norm_num(g.get("number"))
            if n and n not in seen:
                merged.append(g)
                seen.add(n)

        # Clean + normalize
        cleaned = []
        for x in merged:
            n = norm_num(str(x.get("number", "")))
            if not n:
                continue
            cleaned.append({
                "number": n,
                "is_ivr": bool(x.get("is_ivr", False)),
                "ivr_digits": str(x.get("ivr_digits", "") or "").strip(),
                "ivr_wait": int(x.get("ivr_wait", 10) or 10),
                "label": str(x.get("label", "") or "").strip(),
                "confidence": float(x.get("confidence", 0.5) or 0.5),
            })

        data["to_numbers"] = cleaned
        data.setdefault("call_script", "")
        data.setdefault("needs_verification", True)
        data.setdefault("notes", "")

        # If we have directory entries, that’s your “trusted” source
        if directory:
            data["needs_verification"] = False

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
            return json.loads(r.read().decode("utf-8"))

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
            except (HTTPError, URLError, TimeoutError):
                time.sleep(1)
                continue

            if "[inbound_track]" in tr:
                inbound_seen = True

            if not inbound_seen:
                time.sleep(1)
                continue

            # after inbound appears, wait idle window and confirm stable
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

        # timeout fallback
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
        timeout: int = 300
    ) -> str:
        """
        Main entry for Gmail Complaint Warrior:
          - GPT reformulates complaint + proposes numbers
          - we call first number (or you can loop through all)
          - we return agent reply as string
        """
        plan = {} #self.reformulate_for_call(user_complaint, vendor_hint=vendor_hint)
        call_script = plan.get("call_script", "").strip()
        to_numbers = ["+12092084065"] #plan.get("to_numbers", [])

        if not call_script:
            call_script = "Hello. I have a customer complaint and I need help resolving it."

        if not to_numbers:
            return ""

        results = self.make_calls(to_numbers, call_script)
        if not results:
            return ""

        sid = results[0]["call_sid"]
        full = self.wait_for_transcript(sid, overall_timeout=timeout)
        return self.extract_agent_only(full)


if __name__ == "__main__":
    agent = ComplaintCallAgent("config.ini")
    reply = agent.call_and_get_reply_autoroute(
        "I missed my flight connection due to a delay on the first leg. I want reimbursement and compensation.",
        vendor_hint="Southwest",
        timeout=300
    )
    print("\nAGENT REPLY:\n", reply)
