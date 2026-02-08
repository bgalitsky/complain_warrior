# streamlit_app.py
# -*- coding: utf-8 -*-

import os
import time
from pathlib import Path
from typing import List

import streamlit as st

from text_processor import TextProcessing
from complaint_manager import ComplaintWarriorManager, AUTO_SEND_POLICIES, TEST_INBOX_EMAIL

APP_TITLE = "Complaint Warrior — Streamlit"
UPLOAD_DIR = Path("cw_uploads")  # local folder for uploaded evidence


def _ensure_manager():
    """
    Create a singleton manager per Streamlit session.
    Streamlit reruns the script often; we store manager in session_state.
    """
    if "tp" not in st.session_state:
        st.session_state.tp = TextProcessing()

    if "manager" not in st.session_state:
        st.session_state.manager = ComplaintWarriorManager(
            st.session_state.tp,
            log_cb=_log,
        )

    if "polling" not in st.session_state:
        st.session_state.polling = False

    if "selected_complaint_id" not in st.session_state:
        st.session_state.selected_complaint_id = None

    if "selected_thread_id" not in st.session_state:
        st.session_state.selected_thread_id = None

    if "trusted" not in st.session_state:
        st.session_state.trusted = False

    if "mode" not in st.session_state:
        st.session_state.mode = "Manual"

    if "poll_seconds" not in st.session_state:
        st.session_state.poll_seconds = 45

    if "logs" not in st.session_state:
        st.session_state.logs = []


def _log(msg: str):
    st.session_state.logs.append(msg)
    # keep last N lines
    st.session_state.logs = st.session_state.logs[-300:]


def _save_uploads(files) -> List[str]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for uf in files:
        # uf is a streamlit UploadedFile
        dst = UPLOAD_DIR / uf.name
        dst.write_bytes(uf.getbuffer())
        saved.append(str(dst))
    return saved


def _auto_policy_for(trusted: bool) -> str:
    # Before trusted: no auto-send
    # After trusted: auto-send all drafts (you can change to "spawned_only" if you prefer)
    return "all" if trusted else "off"


def _set_policy_on_selected(policy: str):
    cid = st.session_state.selected_complaint_id
    if not cid:
        return
    st.session_state.manager.set_auto_send_policy(cid, policy)


def _manual_poll_once():
    # Manager has internal _poll_once; we use it as an "on-demand check inbox" button.
    st.session_state.manager._poll_once()


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    _ensure_manager()

    st.title(APP_TITLE)
    st.caption(
        f"Test harness: all outgoing mail is sent to **{TEST_INBOX_EMAIL}** "
        f"(intended real recipient kept in subject)."
    )

    # Top controls
    colA, colB, colC, colD = st.columns([1.2, 1.2, 1.2, 1.4])
    with colA:
        st.session_state.mode = st.selectbox("Mode", ["Manual", "Automated"], index=0 if st.session_state.mode == "Manual" else 1)
    with colB:
        st.session_state.trusted = st.toggle(
            "Trusted (allow auto send/check)",
            value=st.session_state.trusted,
            help="Before trusted: you manually send drafts and manually check inbox. After trusted: auto-send policy can be enabled.",
        )
    with colC:
        st.session_state.poll_seconds = st.slider("Poll interval (seconds)", 10, 120, int(st.session_state.poll_seconds), 5)
        # Note: manager uses poll_seconds set at init; we keep manual polling reliable regardless.
    with colD:
        if st.button("Check inbox now (manual poll)"):
            _log("Manual poll requested.")
            try:
                _manual_poll_once()
                st.success("Polled Gmail once.")
            except Exception as e:
                st.error(f"Poll failed: {e}")

    st.divider()

    # Left: create complaint + uploads
    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("New complaint")

        your_email = st.text_input("Your email (for testing)", value="bgalitsky@hotmail.com")
        your_name = st.text_input("Your name", value="Boris Galitsky")
        subject = st.text_input("Subject", value="Reservation Q7K2LM – Request reimbursement for delay ($180 hotel fee) and goodwill credit")
        raw = st.text_area(
            "Raw complaint",
            height=160,
            value="Delayed 4+ hours, lost prepaid hotel cancellation fee $180. Request reimbursement + credit.",
        )

        safe_mode = st.toggle(
            "SAFE MODE (drafts go to self only)",
            value=True,
            help="Your manager already sends to the test inbox; SAFE MODE also informs the LLM not to claim sending to real parties.",
        )

        if st.button("Add complaint", type="primary"):
            try:
                policy = _auto_policy_for(st.session_state.trusted) if st.session_state.mode == "Automated" else "off"
                cs = st.session_state.manager.add_complaint(
                    subject=subject,
                    complaint_raw=raw,
                    user_email=your_email,
                    user_name=your_name,
                    safe_mode=safe_mode,
                    auto_send_policy=policy,
                )
                # Seed a starter thread so it appears immediately (same behavior as Tk UI)
                st.session_state.manager.create_agent_thread_seed(
                    complaint_id=cs.complaint_id,
                    agent_label="merchant_support",
                    parent_thread_id=None,
                    draft_email={"subject": cs.subject, "body": cs.complaint_professional},
                )
                st.session_state.selected_complaint_id = cs.complaint_id
                st.session_state.selected_thread_id = None
                st.success(f"Added complaint {cs.complaint_id}")
            except Exception as e:
                st.error(f"Add complaint failed: {e}")

        st.divider()
        st.subheader("Upload documents")

        cid = st.session_state.selected_complaint_id
        if not cid:
            st.info("Select or add a complaint to attach docs.")
        else:
            up = st.file_uploader(
                "Drop receipts / PDFs / screenshots",
                accept_multiple_files=True,
                type=None,
            )
            if up:
                try:
                    paths = _save_uploads(up)
                    st.session_state.manager.attach_docs(cid, paths)
                    st.success(f"Attached {len(paths)} file(s).")
                except Exception as e:
                    st.error(f"Attach failed: {e}")

            if st.button("Build evidence PDF pack"):
                try:
                    out_pdf = str(UPLOAD_DIR / f"{cid}_evidence_pack.pdf")
                    st.session_state.manager.build_evidence_pdf(cid, out_pdf)
                    st.success(f"Built evidence PDF: {out_pdf}")
                except Exception as e:
                    st.error(f"PDF build failed: {e}")

        st.divider()
        st.subheader("Complaints")

        complaints = st.session_state.manager.list_complaints()
        complaints_sorted = sorted(complaints, key=lambda x: x.created_at, reverse=True)

        options = [f"{cs.complaint_id} | {cs.subject}" for cs in complaints_sorted]
        sel = st.selectbox("Select complaint", ["(none)"] + options, index=0 if not st.session_state.selected_complaint_id else 1)

        if sel != "(none)":
            chosen_cid = sel.split(" | ", 1)[0].strip()
            st.session_state.selected_complaint_id = chosen_cid

        # Auto policy controls
        cid = st.session_state.selected_complaint_id
        if cid:
            cs = st.session_state.manager.get_complaint(cid)

            st.caption("Auto-send policy (per complaint)")
            desired_policy = _auto_policy_for(st.session_state.trusted) if st.session_state.mode == "Automated" else "off"

            # Show and allow override
            pol = st.selectbox("Policy", list(AUTO_SEND_POLICIES), index=list(AUTO_SEND_POLICIES).index(cs.auto_send_policy))
            if st.button("Apply policy"):
                try:
                    st.session_state.manager.set_auto_send_policy(cid, pol)
                    st.success(f"Policy set to {pol}")
                except Exception as e:
                    st.error(f"Set policy failed: {e}")

            st.caption(f"Suggested policy for current mode/trust: **{desired_policy}**")

    # Right: threads / decisions / drafts / inbound
    with right:
        st.subheader("Threads")

        cid = st.session_state.selected_complaint_id
        if not cid:
            st.info("Select a complaint to view threads.")
            st.stop()

        cs = st.session_state.manager.get_complaint(cid)

        thread_items = []
        for tid, ts in cs.threads.items():
            thread_items.append((tid, f"{tid} | {ts.label} | {ts.status}"))
        thread_items.sort(key=lambda x: x[0])

        labels = ["(none)"] + [label for _, label in thread_items]
        sel_thread = st.selectbox("Select thread", labels, index=0 if not st.session_state.selected_thread_id else 1)

        if sel_thread != "(none)":
            tid = sel_thread.split(" | ", 1)[0].strip()
            st.session_state.selected_thread_id = tid

        tid = st.session_state.selected_thread_id
        if not tid:
            st.info("Select a thread to view decisions/drafts/inbound.")
            st.stop()

        ts = cs.threads.get(tid)
        if not ts:
            st.error("Thread missing.")
            st.stop()

        top1, top2 = st.columns([1, 1])
        with top1:
            st.markdown("### Latest GPT decision / plan")
            st.code(ts.last_decision if ts.last_decision else "(No decision yet — waiting for inbound reply)", language="json")

        with top2:
            st.markdown("### Latest inbound (customer support)")
            if st.button("Load latest inbound reply"):
                try:
                    view = st.session_state.manager.load_latest_inbound_view(cid, tid)
                    if not view:
                        st.warning("No inbound reply found.")
                    else:
                        st.success("Inbound loaded.")
                        # refresh local cs/ts
                        cs = st.session_state.manager.get_complaint(cid)
                        ts = cs.threads.get(tid)
                except Exception as e:
                    st.error(f"Load inbound failed: {e}")

            inbound = ts.last_inbound
            if inbound:
                st.text(f"From: {inbound.get('from','')}")
                st.text(f"Subject: {inbound.get('subject','')}")
                st.text(f"Date: {inbound.get('date','')}")
                st.text_area("Body", value=inbound.get("body", ""), height=160)
            else:
                st.caption("(none loaded yet)")

        st.divider()

        st.markdown("### Drafts")
        drafts = ts.drafts or []
        if not drafts:
            st.info("No drafts yet.")
        else:
            draft_labels = []
            for i, d in enumerate(drafts):
                agent = (d.get("to_hint") or ts.label or "unknown").strip()
                subj = (d.get("subject") or "")[:90]
                draft_labels.append(f"{i} | agent={agent} | {subj}")

            pick = st.selectbox("Pick draft", draft_labels)
            i = int(pick.split("|", 1)[0].strip())
            d = drafts[i]

            st.text_input("Subject", value=d.get("subject", ""), disabled=True)
            st.text_area("Body", value=d.get("body", ""), height=220)

            # Manual send always available
            if st.button("Send selected draft (TEST inbox)"):
                try:
                    st.session_state.manager.send_selected_draft_to_self(cid, tid, i)
                    st.success(f"Sent to {TEST_INBOX_EMAIL}")
                except Exception as e:
                    st.error(f"Send failed: {e}")

            # Force draft now (manual)
            if st.button("Draft reply now (GPT-5)"):
                try:
                    _log("Issuing GPT-5 request (manual draft)…")
                    decision = st.session_state.manager.draft_reply_now(cid, tid)
                    if not decision:
                        st.warning("No new (unprocessed) inbound found to draft from.")
                    else:
                        st.success("Draft created.")
                except Exception as e:
                    st.error(f"Draft-now failed: {e}")

        st.divider()
        st.markdown("### Timeline")
        if ts.timeline:
            # show last N events
            for ev in ts.timeline[-200:]:
                st.write(f"{ev.get('ts')} | {ev.get('kind')}: {ev.get('detail')}")
        else:
            st.caption("(empty)")

        st.divider()
        st.markdown("### Logs")
        st.text_area("Log output", value="\n".join(st.session_state.logs[-300:]), height=180)

    # Automated mode: optional auto-poll loop (soft)
    # Note: Streamlit doesn't love background threads beyond your manager's own thread.
    # Here we just hint the user; manager already polls when started, but we keep manual poll as the reliable control.
    if st.session_state.mode == "Automated" and st.session_state.trusted:
        # Ensure policy isn't off (unless user set it explicitly)
        desired = _auto_policy_for(True)
        # If user left it off, we won't override; but we can suggest.
        st.sidebar.success("Automated + Trusted is ON. Use policy controls per complaint to enable auto-send.")
    else:
        st.sidebar.info("Manual mode or not trusted: drafts are not auto-sent; use buttons to send/check inbox.")


if __name__ == "__main__":
    main()
