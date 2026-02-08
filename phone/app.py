import time
import threading
import configparser
from flask import Flask, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client

CONFIG_PATH = "config.ini"

def load_config(path: str) -> configparser.ConfigParser:
    # strict=False tolerates your duplicate [behavior] section
    cfg = configparser.ConfigParser(strict=False)
    read_ok = cfg.read(path, encoding="utf-8")
    if not read_ok:
        raise RuntimeError(f"Could not read config file: {path}")
    return cfg

cfg = load_config(CONFIG_PATH)

ACCOUNT_SID = cfg["twilio"]["account_sid"].strip()
AUTH_TOKEN  = cfg["twilio"]["auth_token"].strip()

VOICE_BASE = cfg["server"]["voice_webhook_base"].strip().rstrip("/")

SILENCE_TIMEOUT = int(cfg.get("behavior", "silence_timeout_seconds", fallback="5"))
MAX_AGENT_SECS  = int(cfg.get("behavior", "max_agent_response_seconds", fallback="180"))
GOODBYE_KWS     = [k.strip().lower() for k in cfg.get("behavior", "goodbye_keywords", fallback="goodbye").split(",")]

VM_PAUSE  = int(cfg.get("behavior", "voicemail_pause_seconds", fallback="2"))
VM_PREFIX = cfg.get("behavior", "voicemail_prefix", fallback="Hello. This is a customer complaint.").strip()

twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN)

app = Flask(__name__)

# In-memory transcript store: CallSid -> text chunks
# For production: use SQLite/Redis.
_TRANSCRIPTS = {}
_LOCK = threading.Lock()

def safe_text(s: str, max_len: int = 2000) -> str:
    return (s or "").strip()[:max_len]

def append_chunk(call_sid: str, text: str, track: str | None = None):
    text = (text or "").strip()
    if not call_sid or not text:
        return
    with _LOCK:
        rec = _TRANSCRIPTS.setdefault(call_sid, {"chunks": [], "updated": time.time()})
        prefix = f"[{track}] " if track else ""
        rec["chunks"].append(prefix + text)
        rec["updated"] = time.time()

def get_transcript(call_sid: str) -> str:
    with _LOCK:
        rec = _TRANSCRIPTS.get(call_sid, {})
        return "\n".join(rec.get("chunks", [])).strip()

def hangup_call(call_sid: str):
    try:
        twilio_client.calls(call_sid).update(status="completed")
    except Exception:
        pass

def update_call_twiml_url(call_sid: str, url: str):
    # Redirect an in-progress call to a different TwiML URL (voicemail flow)
    try:
        twilio_client.calls(call_sid).update(url=url, method="POST")
    except Exception:
        pass

@app.post("/voice_human")
def voice_human():
    """
    Human flow:
      - optional IVR digits
      - say hello + read complaint
      - start real-time transcription
      - record agent response (ends early on silence)
      - hang up
    """
    complaint = safe_text(request.args.get("complaint", ""))
    ivr_digits = (request.args.get("ivr_digits") or "").strip()
    ivr_wait = int(float(request.args.get("ivr_wait", "10")))

    vr = VoiceResponse()

    if not complaint:
        vr.say("Hello. No complaint text was provided. Goodbye.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    # Real-time transcription callback
    start = vr.start()
    start.transcription(
        status_callback_url=f"{VOICE_BASE}/transcription",
        track="both_tracks",
        language_code="en-US",
    )

    # Optional IVR navigation (for 1-800 menus)
    if ivr_digits:
        vr.play(digits=ivr_digits)
        vr.pause(length=max(0, min(60, ivr_wait)))

    # Read complaint
    vr.say("Hello.")
    vr.pause(length=1)
    vr.say(complaint)
    vr.pause(length=1)
    vr.say("I will listen now. Please respond after the tone.")

    # Record response:
    # - timeout ends recording after SILENCE_TIMEOUT seconds of silence
    # - max_length is hard cap
    vr.record(
        action="/recording_done",
        method="POST",
        timeout=max(1, min(30, SILENCE_TIMEOUT)),
        max_length=max(10, min(1800, MAX_AGENT_SECS)),
        play_beep=True
    )

    # Fallback if nothing recorded
    vr.say("No response recorded. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.post("/voice_machine")
def voice_machine():
    """
    Voicemail flow:
      - wait a bit (so greeting finishes)
      - leave message (prefix + complaint)
      - hang up
    """
    complaint = safe_text(request.args.get("complaint", ""))
    vr = VoiceResponse()

    if not complaint:
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    if VM_PAUSE > 0:
        vr.pause(length=min(10, max(0, VM_PAUSE)))

    vr.say(VM_PREFIX)
    vr.pause(length=1)
    vr.say(complaint)
    vr.pause(length=1)
    vr.say("Thank you.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.post("/status")
def status_callback():
    """
    Receives call status + async AMD results.
    If AnsweredBy indicates machine, redirect to voicemail flow (option 2),
    unless the caller told us this target is an IVR (is_ivr=1).
    """
    call_sid = request.values.get("CallSid")
    answered_by = (request.values.get("AnsweredBy") or "").lower()

    # If it's a known IVR target, don't treat it as voicemail.
    is_ivr = (request.args.get("is_ivr") or "0").strip() in ("1", "true", "yes")

    if call_sid and (answered_by.startswith("machine") or answered_by == "fax") and not is_ivr:
        qs = request.query_string.decode("utf-8")
        machine_url = f"{VOICE_BASE}/voice_machine"
        if qs:
            machine_url += "?" + qs
        update_call_twiml_url(call_sid, machine_url)

    return ("", 204)

import json

@app.post("/transcription")
def transcription_callback():
    """
    Twilio Real-Time Transcription statusCallbackUrl webhook.

    Twilio sends events like:
      TranscriptionEvent: transcription-started | transcription-content | transcription-stopped | transcription-error
      TranscriptionData: JSON string e.g. {"transcript":"hello","confidence":0.99}
    """
    # Twilio often sends JSON for real-time transcription events.
    payload = request.get_json(silent=True)
    if payload is None:
        # Fallback for x-www-form-urlencoded
        payload = request.form.to_dict(flat=True) or request.values.to_dict(flat=True)

    call_sid = payload.get("CallSid") or payload.get("call_sid")
    track = payload.get("Track") or payload.get("track")
    event = (payload.get("TranscriptionEvent") or payload.get("transcription_event") or "").lower()
    final_flag = payload.get("Final") or payload.get("final")

    # Extract transcript text from TranscriptionData JSON string
    text = ""
    td = payload.get("TranscriptionData") or payload.get("transcription_data")
    if td:
        try:
            td_obj = json.loads(td) if isinstance(td, str) else td
            text = (td_obj.get("transcript") or "").strip()
        except Exception:
            # If it's not valid JSON, store raw
            text = str(td).strip()

    # Store only content events (but you can store all if you want)
    if call_sid and text and ("content" in event or not event):
        append_chunk(call_sid, text, track=track)

        low = text.lower()
        if any(k in low for k in GOODBYE_KWS):
            hangup_call(call_sid)

    return ("", 204)
@app.post("/recording_done")
def recording_done():
    # You can also store RecordingUrl here if you want the audio.
    vr = VoiceResponse()
    vr.say("Thank you. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")

@app.get("/result/<call_sid>")
def result(call_sid: str):
    return jsonify({
        "call_sid": call_sid,
        "transcript": get_transcript(call_sid)
    })

import time
from flask import jsonify

@app.get("/result/<call_sid>")
def result(call_sid: str):
    """
    Get current transcript snapshot.
    """
    return jsonify({
        "call_sid": call_sid,
        "transcript": get_transcript(call_sid),
        "updated": _TRANSCRIPTS.get(call_sid, {}).get("updated")
    })


@app.get("/wait_result/<call_sid>")
def wait_result(call_sid: str):
    """
    Long-poll-ish retrieval:
    Wait until transcript changes OR timeout.
    Query params:
      - timeout: seconds to wait (default 30, max 120)
      - min_chars: require at least N characters before returning (default 1)
      - idle: return early if no updates for N seconds (default 10)
    """
    timeout = float(request.args.get("timeout", "30"))
    timeout = max(1.0, min(120.0, timeout))

    min_chars = int(request.args.get("min_chars", "1"))
    min_chars = max(0, min(100000, min_chars))

    idle = float(request.args.get("idle", "10"))
    idle = max(1.0, min(60.0, idle))

    start = time.time()

    with _LOCK:
        last_updated = _TRANSCRIPTS.get(call_sid, {}).get("updated", 0.0)

    while True:
        tr = get_transcript(call_sid)
        now = time.time()

        with _LOCK:
            upd = _TRANSCRIPTS.get(call_sid, {}).get("updated", 0.0)

        # return if we have enough text and it changed since the request started
        if len(tr) >= min_chars and upd > last_updated:
            return jsonify({"call_sid": call_sid, "transcript": tr, "updated": upd})

        # return if idle for too long (no new chunks)
        if upd and (now - upd) >= idle and len(tr) >= min_chars:
            return jsonify({"call_sid": call_sid, "transcript": tr, "updated": upd, "idle": True})

        # timeout
        if (now - start) >= timeout:
            return jsonify({"call_sid": call_sid, "transcript": tr, "updated": upd, "timeout": True})

        time.sleep(0.5)


if __name__ == "__main__":
    # Local run: python app.py
    app.run(host="0.0.0.0", port=5000, debug=True)
