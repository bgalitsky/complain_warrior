import configparser
from urllib.parse import urlencode
from typing import List, Union, Dict, Any
from twilio.rest import Client

CONFIG_PATH = "config.ini"

def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(strict=False)
    ok = cfg.read(path, encoding="utf-8")
    if not ok:
        raise RuntimeError(f"Could not read config file: {path}")
    return cfg

cfg = load_config(CONFIG_PATH)

ACCOUNT_SID = cfg["twilio"]["account_sid"].strip()
AUTH_TOKEN  = cfg["twilio"]["auth_token"].strip()
FROM_NUMBER = cfg["twilio"]["from_number"].strip()
BASE        = cfg["server"]["voice_webhook_base"].strip().rstrip("/")
AMD_MODE    = cfg.get("behavior", "amd_mode", fallback="DetectMessageEnd").strip()

client = Client(ACCOUNT_SID, AUTH_TOKEN)

def make_calls(to_numbers: List[Union[str, Dict[str, Any]]], complaint: str):
    """
    to_numbers can be:
      ["+1209....", "+1800...."]
    OR:
      [
        {"number":"+1800....", "is_ivr": True, "ivr_digits":"wwww0ww1", "ivr_wait": 10},
        {"number":"+1209...."}
      ]

    Notes:
      - For IVR targets set is_ivr=True so AMD 'machine' won't trigger voicemail flow.
      - For non-IVR targets, AMD 'machine' will trigger voicemail flow automatically.
    """
    results = []

    for entry in to_numbers:
        if isinstance(entry, str):
            number = entry
            ivr_digits = ""
            ivr_wait = 10
            is_ivr = False
        else:
            number = entry["number"]
            ivr_digits = entry.get("ivr_digits", "")
            ivr_wait = int(entry.get("ivr_wait", 10))
            is_ivr = bool(entry.get("is_ivr", False))

        params = {"complaint": complaint}
        if ivr_digits:
            params["ivr_digits"] = ivr_digits
            params["ivr_wait"] = str(ivr_wait)
        if is_ivr:
            params["is_ivr"] = "1"

        qs = urlencode(params)

        voice_url = f"{BASE}/voice_human?{qs}"
        status_url = f"{BASE}/status?{qs}"

        call = client.calls.create(
            to=number,
            from_=FROM_NUMBER,
            url=voice_url,
            method="POST",

            machine_detection=AMD_MODE,
            async_amd=True,

            status_callback=status_url,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"]
        )

        results.append({"to": number, "call_sid": call.sid})
        print(f"Calling {number} -> CallSid {call.sid}")
        print(f"Transcript: {BASE}/result/{call.sid}")
        print("-----")

    return results

import json
import time
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

def fetch_transcript(base_url: str, call_sid: str) -> str:
        """
        One-shot snapshot.
        """
        url = f"{base_url.rstrip('/')}/result/{call_sid}"
        with urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("transcript", "") or ""

import json
import time
from urllib.request import urlopen

import json
import time
from urllib.request import urlopen

def wait_for_transcript(
    base_url: str,
    call_sid: str,
    overall_timeout: int = 300,
    require_inbound: bool = True,
    idle_seconds_after_inbound: int = 6,
):
    """
    Waits until:
      - inbound_track appears (if require_inbound=True), then
      - transcript becomes idle for idle_seconds_after_inbound
    Returns transcript string.
    """

    base = base_url.rstrip("/")
    deadline = time.time() + overall_timeout

    inbound_seen = False

    def fetch():
        url = f"{base}/result/{call_sid}"
        with urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    last_transcript = ""

    while time.time() < deadline:
        data = fetch()
        tr = data.get("transcript", "") or ""

        if require_inbound and "[inbound_track]" in tr:
            inbound_seen = True

        # If inbound required but not yet seen, keep waiting
        if require_inbound and not inbound_seen:
            time.sleep(1)
            continue

        # Wait a short idle window to ensure agent finished speaking
        time.sleep(idle_seconds_after_inbound)
        data2 = fetch()
        tr2 = data2.get("transcript", "") or ""

        if tr2 == tr:
            return tr2

        last_transcript = tr2
        time.sleep(1)

    # Timeout fallback
    return fetch().get("transcript", "") or ""

def extract_agent_only(full_transcript: str) -> str:
    lines = []
    for line in (full_transcript or "").splitlines():
        if line.startswith("[inbound_track]"):
            lines.append(line.replace("[inbound_track]", "").strip())
    return "\n".join(lines).strip()



if __name__ == "__main__":
    complaint_text = (
        "I am calling regarding a missed flight connection caused by a delay on the first leg. "
      #  "Because of the delay, I missed my connection and arrived significantly late. "
      #  "I received inadequate rebooking support and incurred additional expenses. "
      #  "I am requesting reimbursement for reasonable expenses and appropriate compensation."
    )

    # Example list that another module (GPT router) would produce:
    to_list = [
        # IVR number: navigate menus, do NOT treat AMD 'machine' as voicemail
        #{"number": "+18005551234", "is_ivr": True, "ivr_digits": "wwww0ww1", "ivr_wait": 10},

        # Direct line: if voicemail detected, leave voicemail
         "+12092084065",
    ]

    results = make_calls(to_list, complaint_text)

    if not results:
        print("No calls were created.")
    else:
        sid = results[0]["call_sid"]

        print("Waiting for transcript...")

        full = wait_for_transcript(BASE, sid, require_inbound=True)
        agent = extract_agent_only(full)
        print("AGENT ONLY:\n", agent)