
# cw_app_phone_refactored.py
# -*- coding: utf-8 -*-
import os
import sqlite3
from pathlib import Path
from typing import List

import streamlit as st

from text_processor import TextProcessing
from complaint_manager_phone import ComplaintWarriorManager, AUTO_SEND_POLICIES, TEST_INBOX_EMAIL

APP_TITLE = "Complaint Warrior"
UPLOAD_DIR = Path("cw_uploads")
TOKEN_DB = os.environ.get("GMAIL_TOKEN_DB", "cw_gmail_tokens.sqlite")
LOGO_PATH = os.environ.get("CW_LOGO_PATH", "complaint_warrior.png")


def _log(msg: str):
    if "logs" not in st.session_state:
        st.session_state.logs = []
    st.session_state.logs.append(msg)
    st.session_state.logs = st.session_state.logs[-500:]

def _init_state():
    if "logs" not in st.session_state:
        st.session_state.logs = []
    if "manager" not in st.session_state:
        st.session_state.manager = None


def _list_gmail_token_keys(db_path: str) -> List[str]:
    try:
        con = sqlite3.connect(db_path)
        con.execute("""
          CREATE TABLE IF NOT EXISTS gmail_tokens(
            key TEXT PRIMARY KEY,
            token_json TEXT NOT NULL,
            updated_at REAL NOT NULL
          )
        """)
        rows = con.execute("SELECT key FROM gmail_tokens ORDER BY updated_at DESC").fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _ensure_manager():
    if "logs" not in st.session_state:
        st.session_state.logs = []
    if "tp" not in st.session_state:
        st.session_state.tp = TextProcessing()
    if "manager" not in st.session_state or st.session_state.manager is None:
        st.session_state.manager = ComplaintWarriorManager(st.session_state.tp, log_cb=_log)
    if "app_user_email" not in st.session_state:
        st.session_state.app_user_email = ""
    if "gmail_user_key" not in st.session_state:
        st.session_state.gmail_user_key = "default"
    if "selected_complaint_id" not in st.session_state:
        st.session_state.selected_complaint_id = None
    if "selected_thread_id" not in st.session_state:
        st.session_state.selected_thread_id = None
    if "mode" not in st.session_state:
        st.session_state.mode = "manual"
    if "trusted" not in st.session_state:
        st.session_state.trusted = False


def _save_uploads(files) -> List[str]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for uf in files or []:
        dst = UPLOAD_DIR / uf.name
        dst.write_bytes(uf.getbuffer())
        saved.append(str(dst))
    return saved


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    _init_state()
    _ensure_manager()

    # Header / logo
    c1, c2 = st.columns([0.14, 0.86])
    with c1:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width=110)
    with c2:
        st.title(APP_TITLE)
        st.caption("Logical sequence: initial demand → negotiation → resolution check → resolved/escalated. Email and phone activity are shown together.")

    # Sidebar: unchanged Gmail auth entry point
    PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
    with st.sidebar:
        st.header("Gmail")
        if PUBLIC_BASE:
            st.markdown(f"[Connect Gmail]({PUBLIC_BASE}/auth/start)")
        else:
            st.warning("PUBLIC_BASE is not set (e.g. your ngrok URL). Set it so the Connect button can work.")

        keys = _list_gmail_token_keys(TOKEN_DB)
        if keys:
            st.session_state.gmail_user_key = st.selectbox("Connected Gmail identity", keys, index=0)
            try:
                st.session_state.manager.set_gmail_user(st.session_state.gmail_user_key)
                st.success(f"Using Gmail key: {st.session_state.gmail_user_key}")
            except Exception as e:
                st.error(str(e))
        else:
            st.info("No Gmail tokens found yet.")

        st.divider()
        st.header("App user")
        st.session_state.app_user_email = st.text_input("Your email", value=st.session_state.app_user_email, placeholder="name@gmail.com").strip().lower()
        if st.button("Use this account"):
            try:
                st.session_state.manager.set_user(st.session_state.app_user_email)
                st.success(f"Active user: {st.session_state.app_user_email}")
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.header("Execution mode")
        st.session_state.mode = st.radio("Mode", ["manual", "automated"], index=0 if st.session_state.mode == "manual" else 1)
        st.session_state.trusted = st.toggle("Trusted", value=st.session_state.trusted, help="Trusted enables automatic sending plus GPT satisfaction fallback on mixed signals only.")

        if st.button("Run one automated step"):
            try:
                st.session_state.manager.automated_step(trusted=st.session_state.trusted)
                st.success("Automated step completed.")
                st.rerun()
            except Exception as e:
                st.error(f"Automated step failed: {e}")

    if st.session_state.manager is None or not st.session_state.manager.user_email:
        st.info("In the sidebar, set your app email and click **Use this account**.")
        st.stop()

    # Left: new complaint / attachments
    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("1) Initial demand")
        your_name = st.text_input("Your name", value="Boris Galitsky")
        subject = st.text_input("Subject", value="Complaint regarding missed connection and reimbursement")
        raw = st.text_area("Describe the complaint", height=180, value="My first flight was delayed, I missed my connection, and I had to pay for hotel and meals. I request reimbursement of those costs.")

        policy_default = "auto_send" if (st.session_state.mode == "automated" and st.session_state.trusted) else "manual"
        auto_policy = st.selectbox("Communication policy", list(AUTO_SEND_POLICIES), index=list(AUTO_SEND_POLICIES).index(policy_default))
        if st.button("Create complaint", type="primary"):
            try:
                cs = st.session_state.manager.add_complaint(
                    subject=subject,
                    complaint_raw=raw,
                    user_email=st.session_state.app_user_email,
                    user_name=your_name,
                    auto_send_policy=auto_policy,
                )
                st.session_state.selected_complaint_id = cs.complaint_id
                st.session_state.selected_thread_id = next(iter(cs.threads.keys()))
                st.success(f"Created complaint {cs.complaint_id}")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.subheader("Documents")
        cid = st.session_state.selected_complaint_id
        if cid:
            files = st.file_uploader("Upload receipts / screenshots / PDFs", accept_multiple_files=True)
            if files:
                saved = _save_uploads(files)
                st.session_state.manager.attach_docs(cid, saved)
                st.success(f"Uploaded {len(saved)} file(s).")
            if st.button("Build evidence PDF"):
                out = str(UPLOAD_DIR / f"{cid}_evidence.pdf")
                st.session_state.manager.build_evidence_pdf(cid, out)
                st.success("Evidence PDF generated.")
        else:
            st.caption("Create/select a complaint first.")

    with right:
        complaints = st.session_state.manager.list_complaints()
        if not complaints:
            st.info("No complaints yet for this user.")
            st.stop()

        complaint_ids = [c.complaint_id for c in complaints]
        idx = complaint_ids.index(st.session_state.selected_complaint_id) if st.session_state.selected_complaint_id in complaint_ids else 0
        cid = st.selectbox("Complaint", complaint_ids, index=idx)
        st.session_state.selected_complaint_id = cid
        cs = st.session_state.manager.get_complaint(cid)

        # resolution strategy/status
        st.subheader("2) Resolution strategy and current status")
        s1, s2, s3 = st.columns(3)

        with s1:
            st.caption("Primary goal")
            st.write(cs.strategy.get("primary_goal", "") or "—")

        with s2:
            st.caption("Current status")
            st.write(cs.current_status_summary or "—")

        with s3:
            st.caption("Conclusion")
            st.write(cs.final_conclusion or "Open")
        with st.expander("Resolution strategy detail", expanded=True):
            st.write({
                "primary_goal": cs.strategy.get("primary_goal"),
                "acceptable_fallbacks": cs.strategy.get("acceptable_fallbacks"),
                "escalate_if": cs.strategy.get("escalate_if"),
                "evidence_needed": cs.strategy.get("evidence_needed"),
                "communication_policy": cs.auto_send_policy,
            })

        # thread selection
        thread_ids = list(cs.threads.keys())
        t_idx = thread_ids.index(st.session_state.selected_thread_id) if st.session_state.selected_thread_id in thread_ids else 0
        tid = st.selectbox("Thread / lane", thread_ids, index=t_idx)
        st.session_state.selected_thread_id = tid
        ts = cs.threads[tid]
        st.caption(f"Thread label: {ts.label} | Stage: {ts.stage} | Status: {ts.status}")

        # clear sequencing
        st.subheader("3) Negotiation step")
        b1, b2 = st.columns([1, 1])

        with b1:
            if st.button("Draft next message", type="primary"):
                try:
                    st.session_state.manager.draft_reply_now(cid, tid)
                    st.success("Draft generated.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        drafts = ts.drafts or []
        selected_idx = []
        if drafts:
            opts = [f"[{i}] to={d.get('to_hint','')} | {d.get('subject','')[:90]}" for i, d in enumerate(drafts)]
            picked = st.multiselect("Drafts ready to send", opts, default=[opts[0]])
            selected_idx = [int(x.split("]")[0][1:]) for x in picked]
            if selected_idx:
                st.text_area("Draft preview", value=drafts[selected_idx[0]].get("body", ""), height=220)

        with b2:
            if st.button("Send selected drafts"):
                try:
                    st.session_state.manager.send_selected_drafts(cid, tid, selected_idx or [0], attachments=cs.docs)
                    st.success("Sent.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.subheader("4) Phone contact")
        if st.button("Call company now"):
            try:
                reply = st.session_state.manager.place_phone_call_and_capture_reply(cid, tid)
                st.success("Phone reply captured.")
                st.text_area("Phone reply", reply, height=180)
                st.rerun()
            except Exception as e:
                st.error(f"Phone call failed: {e}")

        st.subheader("5) Combined activity log")
        for ev in reversed(cs.activities[-50:]):
            with st.container(border=True):
                st.markdown(f"**{ev['ts']}** — `{ev['channel']}` / `{ev['kind']}` — **{ev['title']}**")
                st.write(ev["detail"])
                if ev.get("meta"):
                    with st.expander("metadata"):
                        st.json(ev["meta"])

    st.divider()
    st.subheader("Decision / processor log")
    st.code("\n".join(st.session_state.logs[-250:]) if st.session_state.logs else "(no logs yet)", language="text")


if __name__ == "__main__":
    main()
