# app.py
import json
import time
import threading
import configparser
from typing import Optional, Dict, Any

from flask import Flask, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client


class PhoneWebhookServer:
    """
    Flask server that Twilio calls for:
      - voice instructions (human/machine)
      - AMD status callbacks
      - real-time transcription callbacks
      - transcript retrieval
    """

    def __init__(self, config_path: str = "config.ini"):
        self.cfg = configparser.ConfigParser(strict=False)
        ok = self.cfg.read(config_path, encoding="utf-8")
        if not ok:
            raise RuntimeError(f"Could not read config file: {config_path}")

        # Twilio creds (used only for hangup / redirect mid-call)
        self.account_sid = self.cfg["twilio"]["account_sid"].strip()
        self.auth_token = self.cfg["twilio"]["auth_token"].strip()
        self.twilio = Client(self.account_sid, self.auth_token)

        # Public base URL (must be HTTPS and reachable by Twilio)
        self.public_base = self.cfg["server"]["voice_webhook_base"].strip().rstrip("/")

        # Behavior knobs
        self.silence_timeout = int(self.cfg.get("behavior", "silence_timeout_seconds", fallback="5"))
        self.max_agent_secs = int(self.cfg.get("behavior", "max_agent_response_seconds", fallback="180"))

        self.goodbye_kws = [
            k.strip().lower()
            for k in self.cfg.get("behavior", "goodbye_keywords", fallback="goodbye,bye").split(",")
            if k.strip()
        ]

        self.vm_pause = int(self.cfg.get("behavior", "voicemail_pause_seconds", fallback="2"))
        self.vm_prefix = self.cfg.get(
            "behavior",
            "voicemail_prefix",
            fallback="Hello. This is a customer complaint."
        ).strip()

        # Transcript store (in-memory). For persistence: SQLite/Redis.
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}  # CallSid -> {"chunks":[...], "updated": float}

        self.app = Flask(__name__)
        self._routes()

    # -------------------- storage helpers --------------------

    def _append(self, call_sid: str, text: str, track: Optional[str]):
        text = (text or "").strip()
        if not call_sid or not text:
            return
        with self._lock:
            rec = self._store.setdefault(call_sid, {"chunks": [], "updated": time.time()})
            prefix = f"[{track}] " if track else ""
            rec["chunks"].append(prefix + text)
            rec["updated"] = time.time()

    def transcript(self, call_sid: str) -> str:
        with self._lock:
            rec = self._store.get(call_sid, {})
            return "\n".join(rec.get("chunks", [])).strip()

    def updated(self, call_sid: str) -> Optional[float]:
        with self._lock:
            rec = self._store.get(call_sid)
            return rec.get("updated") if rec else None

    # -------------------- twilio control helpers --------------------

    def _hangup(self, call_sid: str):
        try:
            self.twilio.calls(call_sid).update(status="completed")
        except Exception:
            pass

    def _redirect_call(self, call_sid: str, url: str):
        try:
            self.twilio.calls(call_sid).update(url=url, method="POST")
        except Exception:
            pass

    # -------------------- routes --------------------

    def _routes(self):
        app = self.app

        @app.get("/health")
        def health():
            return jsonify({"ok": True})

        @app.post("/voice_human")
        def voice_human():
            """
            Human/IVR flow:
              - optional IVR digits
              - say hello + read complaint
              - start real-time transcription
              - record agent response (stops on silence)
            """
            complaint = (request.args.get("complaint") or "").strip()
            ivr_digits = (request.args.get("ivr_digits") or "").strip()
            ivr_wait = int(float(request.args.get("ivr_wait", "10")))

            complaint = complaint[:2500]  # keep TwiML reasonable

            vr = VoiceResponse()

            if not complaint:
                vr.say("Hello. No complaint text was provided. Goodbye.")
                vr.hangup()
                return Response(str(vr), mimetype="text/xml")

            # Start real-time transcription -> /transcription
            start = vr.start()
            start.transcription(
                status_callback_url=f"{self.public_base}/transcription",
                track="both_tracks",
                language_code="en-US",
            )

            # Optional IVR navigation (do not speak before tones)
            if ivr_digits:
                vr.play(digits=ivr_digits)
                vr.pause(length=max(0, min(60, ivr_wait)))

            # Read complaint
            vr.say("Hello.")
            vr.pause(length=1)
            vr.say(complaint)
            vr.pause(length=1)
            vr.say("I will listen now. Please respond after the tone.")

            # Record response (silence timeout ends early)
            vr.record(
                action="/recording_done",
                method="POST",
                timeout=max(1, min(30, self.silence_timeout)),
                max_length=max(10, min(1800, self.max_agent_secs)),
                play_beep=True
            )

            # Fallback
            vr.say("No response recorded. Goodbye.")
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        @app.post("/voice_machine")
        def voice_machine():
            """
            Voicemail flow:
              - short pause so greeting finishes
              - leave voicemail message (prefix + complaint)
              - hang up
            """
            complaint = (request.args.get("complaint") or "").strip()[:2500]
            vr = VoiceResponse()
            if not complaint:
                vr.hangup()
                return Response(str(vr), mimetype="text/xml")

            vr.pause(length=min(10, max(0, self.vm_pause)))
            vr.say(self.vm_prefix)
            vr.pause(length=1)
            vr.say(complaint)
            vr.pause(length=1)
            vr.say("Thank you.")
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        @app.post("/status")
        def status():
            """
            Call status + async AMD result.
            If AnsweredBy indicates machine and not is_ivr -> redirect to voicemail flow.
            """
            call_sid = request.values.get("CallSid")
            answered_by = (request.values.get("AnsweredBy") or "").lower()
            is_ivr = (request.args.get("is_ivr") or "0").strip().lower() in ("1", "true", "yes")

            if call_sid and (answered_by.startswith("machine") or answered_by == "fax") and not is_ivr:
                qs = request.query_string.decode("utf-8")
                url = f"{self.public_base}/voice_machine"
                if qs:
                    url += "?" + qs
                self._redirect_call(call_sid, url)

            return ("", 204)

        @app.post("/transcription")
        def transcription():
            """
            Real-time transcription callback.
            Twilio sends JSON events with:
              - TranscriptionEvent
              - TranscriptionData (JSON string containing 'transcript')
            We store transcript chunks and hang up if goodbye keywords detected.
            """
            payload = request.get_json(silent=True)
            if payload is None:
                payload = request.form.to_dict(flat=True) or request.values.to_dict(flat=True)

            call_sid = payload.get("CallSid") or payload.get("call_sid")
            track = payload.get("Track") or payload.get("track")

            event = (payload.get("TranscriptionEvent") or payload.get("transcription_event") or "").lower()

            text = ""
            td = payload.get("TranscriptionData") or payload.get("transcription_data")
            if td:
                try:
                    td_obj = json.loads(td) if isinstance(td, str) else td
                    text = (td_obj.get("transcript") or "").strip()
                except Exception:
                    text = str(td).strip()

            # store only "content" events (or store all if event empty)
            if call_sid and text and ("content" in event or not event):
                self._append(call_sid, text, track)

                low = text.lower()
                if any(k in low for k in self.goodbye_kws):
                    self._hangup(call_sid)

            return ("", 204)

        @app.post("/recording_done")
        def recording_done():
            vr = VoiceResponse()
            vr.say("Thank you. Goodbye.")
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        @app.get("/result/<call_sid>")
        def result(call_sid: str):
            return jsonify({
                "call_sid": call_sid,
                "transcript": self.transcript(call_sid),
                "updated": self.updated(call_sid),
            })

    def run(self, host: str = "0.0.0.0", port: int = 5000, debug: bool = True):
        self.app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    PhoneWebhookServer("config.ini").run()
