
# cw_app_phone_refactored.py
# -*- coding: utf-8 -*-
import os
import sqlite3
from pathlib import Path
from typing import List
import time

import streamlit as st

from text_processor import TextProcessing
from complaint_manager_phone import (
    ComplaintWarriorManager,
    AUTO_SEND_POLICIES,
    TEST_INBOX_EMAIL,
    COMPLAINT_MODULE_STATUSES,
)

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

def _selected_complaint_is_waiting_subscribed_flow() -> bool:
    cid = st.session_state.get("selected_complaint_id")
    mgr = st.session_state.get("manager")
    if not cid or mgr is None:
        return False
    try:
        cs = mgr.get_complaint(cid)
        return (cs.current_status_summary or "").strip().lower() == \
               "expecting the settlement from the company. wait for their response"
    except Exception:
        return False


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

SUBSCRIPTION_DB = os.environ.get("CW_SUBSCRIPTIONS_DB", "cw_companies.sqlite")

def _normalize_company_name(name: str) -> str:
    return " ".join((name or "").strip().split())


def _is_company_subscribed(company_name: str, db_path: str = SUBSCRIPTION_DB) -> bool:
    company_name = _normalize_company_name(company_name)
    if not company_name:
        return False

    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS subscribed_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL UNIQUE,
            contact_name TEXT,
            contact_email TEXT,
            contact_phone TEXT,
            subscription_tier TEXT DEFAULT 'standard',
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    row = con.execute("""
        SELECT is_active
        FROM subscribed_companies
        WHERE lower(company_name) = lower(?)
    """, (company_name,)).fetchone()
    con.close()
    return bool(row and int(row[0]) == 1)

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


def _normalize_module_statuses(raw):
    statuses = {
        key: {"done": False, "label": label, "updated_at": None, "note": ""}
        for key, label in COMPLAINT_MODULE_STATUSES.items()
    }
    for key, value in (raw or {}).items():
        if key not in statuses:
            continue
        if isinstance(value, dict):
            statuses[key].update(value)
            statuses[key]["label"] = COMPLAINT_MODULE_STATUSES[key]
        else:
            statuses[key]["done"] = bool(value)
    return statuses


def _status_icon(done: bool) -> str:
    return "✅" if done else "⬜"


def _render_next_recommendation(manager, cid: str, tid: str):
    """Show and optionally apply the manager's next resolution recommendation."""
    try:
        rec = manager.recommend_next_resolution_status(cid, tid)
    except Exception as e:
        st.warning(f"Could not compute recommendation: {e}")
        return

    title = rec.get("title") or "Next recommendation"
    reason = rec.get("reason") or ""
    action = rec.get("action") or ""
    recommended_status = rec.get("recommended_status") or ""
    blocked = bool(rec.get("blocked"))

    if recommended_status == "resolved":
        st.success(f"**Recommendation:** {title}\n\n{reason}\n\n**Action:** {action}")
    elif blocked:
        st.info(f"**Recommendation:** {title}\n\n{reason}\n\n**Action:** {action}")
    else:
        st.success(f"**Recommendation:** {title}\n\n{reason}\n\n**Action:** {action}")

    if recommended_status in COMPLAINT_MODULE_STATUSES and not blocked:
        if st.button(
            f"Mark as: {COMPLAINT_MODULE_STATUSES[recommended_status]}",
            key=f"apply_recommended_status_{cid}_{recommended_status}",
        ):
            try:
                manager.set_module_status(
                    cid,
                    recommended_status,
                    True,
                    f"Applied from next-step recommendation: {title}",
                )
                st.success("Recommended module status applied.")
                st.rerun()
            except Exception as e:
                st.error(str(e))


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
    AUTH_GUIDE_URL = os.environ.get(
        "CW_AUTH_GUIDE_URL",
        f"{PUBLIC_BASE}/downloads/Gmail%20Authentication.pdf"
    )
    with st.sidebar:
        st.header("Gmail")
        if PUBLIC_BASE:
            st.markdown(f"[Connect Gmail]({PUBLIC_BASE}/auth/start)")
            st.caption("First time setup")
            st.markdown(f"📄 [Gmail Authentication Instructions (PDF)]({AUTH_GUIDE_URL})")
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
                if _selected_complaint_is_waiting_subscribed_flow():
                    st.info(
                        "This complaint is waiting for company settlement response. No further automated action is taken.")
                else:
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
        st.markdown("### Quick Actions")
        st.markdown(f"- [Charge Back Initiator]({os.environ.get('CW_CHARGE_BACK_APP_URL', '/charge_back_initiator/')})")
        st.markdown(f"- [CW Regulatory]({os.environ.get('CW_REGULATORY_APP_URL', '/cw_regulatory/')})")
        st.markdown(f"- [Small Claim Court Warrior]({os.environ.get('CW_SMALL_CLAIMS_APP_URL', '/small_claim_court_warrior/')})")
        social_url = os.environ.get("CW_SOCIAL_SHARE_URL", "")
        if social_url:
            st.markdown(f"- [Social Network Poster]({social_url})")

        your_name = st.text_input("Your name", value="Boris Galitsky")
        company_name = st.text_input("Company name", value="")
        subject = st.text_input("Subject", value="Complaint regarding missed connection and reimbursement")
        raw = st.text_area("Describe the complaint", height=180, value="My first flight was delayed, I missed my connection, and I had to pay for hotel and meals. I request reimbursement of those costs.")

        policy_default = "auto_send" if (st.session_state.mode == "automated" and st.session_state.trusted) else "manual"
        auto_policy = st.selectbox("Communication policy", list(AUTO_SEND_POLICIES), index=list(AUTO_SEND_POLICIES).index(policy_default))
        if st.button("Create complaint", type="primary"):
            try:
                subscribed = _is_company_subscribed(company_name)

                cs = st.session_state.manager.add_complaint(
                    subject=subject,
                    complaint_raw=raw,
                    user_email=st.session_state.app_user_email,
                    user_name=your_name,
                    auto_send_policy=auto_policy,
                )

                st.session_state.selected_complaint_id = cs.complaint_id
                tid = next(iter(cs.threads.keys()))
                st.session_state.selected_thread_id = tid

                if subscribed:
                    # Send only the first complaint email
                    st.session_state.manager.draft_reply_now(cs.complaint_id, tid)
                    st.session_state.manager.send_selected_drafts(cs.complaint_id, tid, [0], attachments=cs.docs)

                    # Set current status summary and conclusion/state for subscribed companies
                    cs.current_status_summary = "expecting the settlement from the company. Wait for their response"
                    cs.final_conclusion = "Awaiting company response"

                    # Optional: mark thread stage/status if those fields exist and are mutable
                    try:
                        ts = cs.threads[tid]
                        ts.status = "awaiting_company_response"
                        ts.stage = "waiting_for_settlement"
                    except Exception:
                        pass

                    # Optional: log activity event if activities list is available
                    try:
                        cs.activities.append({
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "channel": "system",
                            "kind": "status",
                            "title": "Subscribed company flow activated",
                            "detail": "First complaint email sent. Waiting for settlement response from company.",
                            "meta": {
                                "company_name": company_name,
                                "subscription_db": SUBSCRIPTION_DB,
                            },
                        })
                    except Exception:
                        pass

                    st.success(
                        f"Created complaint {cs.complaint_id}. "
                        "Company is subscribed: first email sent, now waiting for response."
                    )
                else:
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

        complaint_labels = []
        complaint_id_by_label = {}

        for c in complaints:
            title = getattr(c, "subject", "") or getattr(c, "title", "") or "Untitled complaint"
            label = f"{c.complaint_id} — {title}"
            complaint_labels.append(label)
            complaint_id_by_label[label] = c.complaint_id

        current_label = None
        for label, complaint_id in complaint_id_by_label.items():
            if complaint_id == st.session_state.selected_complaint_id:
                current_label = label
                break

        idx = complaint_labels.index(current_label) if current_label in complaint_labels else 0
        selected_label = st.selectbox("Complaint", complaint_labels, index=idx)

        cid = complaint_id_by_label[selected_label]
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

        st.markdown("#### Module resolution status")
        module_statuses = _normalize_module_statuses(getattr(cs, "module_statuses", {}))
        status_cols = st.columns(min(5, max(1, len(module_statuses))))
        for idx, (status_key, status_info) in enumerate(module_statuses.items()):
            with status_cols[idx % len(status_cols)]:
                st.write(f"{_status_icon(status_info.get('done'))} {status_info.get('label')}")
                if status_info.get("updated_at"):
                    st.caption(status_info.get("updated_at"))

        with st.expander("Update module status", expanded=False):
            c_status, c_note = st.columns([1, 2])
            with c_status:
                status_choice = st.selectbox(
                    "Status",
                    list(COMPLAINT_MODULE_STATUSES.keys()),
                    format_func=lambda k: COMPLAINT_MODULE_STATUSES[k],
                )
                status_done = st.checkbox("Completed", value=True)
            with c_note:
                status_note = st.text_input("Optional note", value="")
                if st.button("Save module status"):
                    try:
                        st.session_state.manager.set_module_status(cid, status_choice, status_done, status_note)
                        st.success("Module status updated.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

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

        pause_reason = st.session_state.manager.pause_reason(cs)
        actions_paused = bool(pause_reason)
        if actions_paused:
            st.warning(pause_reason)

        st.markdown("#### Overall check and next recommended action")
        _render_next_recommendation(st.session_state.manager, cid, tid)

        # clear sequencing
        st.subheader("3) Negotiation step")
        b1, b2 = st.columns([1, 1])

        with b1:
            if st.button("Draft next message", type="primary", disabled=actions_paused):
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
            if st.button("Send selected drafts", disabled=actions_paused):
                try:
                    st.session_state.manager.send_selected_drafts(cid, tid, selected_idx or [0], attachments=cs.docs)
                    st.success("Sent.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.subheader("4) Phone contact")
        if st.button("Call company now", disabled=actions_paused):
            try:
                reply = st.session_state.manager.place_phone_call_and_capture_reply(cid, tid)
                if reply and reply.strip():
                    st.success("Phone reply captured and overall recommendation updated.")
                    st.text_area("Phone reply", reply, height=180)
                else:
                    st.warning("Call completed, but no phone reply transcript was captured. Overall recommendation was not escalated.")
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
