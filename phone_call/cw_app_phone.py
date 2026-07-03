
# cw_app_phone_refactored.py
# -*- coding: utf-8 -*-
import os
import re
import json
import sqlite3
from pathlib import Path
from typing import List, Optional
import time

import streamlit as st
import streamlit.components.v1 as components

from text_processor import TextProcessing
from complaint_manager_phone import (
    ComplaintWarriorManager,
    AUTO_SEND_POLICIES,
    TEST_INBOX_EMAIL,
    COMPLAINT_MODULE_STATUSES,
    outbound_email_mode_label,
)

APP_TITLE = "Complaint Warrior"
UPLOAD_DIR = Path("cw_uploads")
TOKEN_DB = os.environ.get("GMAIL_TOKEN_DB", "cw_gmail_tokens.sqlite")
LOGO_PATH = os.environ.get("CW_LOGO_PATH", "complaint_warrior.png")


ASSISTANT_NAVIGATION = {
    "new_complaint": {
        "label": "Create a new complaint",
        "kind": "section",
        "anchor": "cw-initial-demand",
        "description": "Clears section 1 and opens the fields used to create a complaint.",
    },
    "initial_demand": {
        "label": "Initial demand",
        "kind": "section",
        "anchor": "cw-initial-demand",
        "description": "Shows the complaint description, company identity, subject, and communication policy.",
    },
    "documents": {
        "label": "Documents and evidence",
        "kind": "section",
        "anchor": "cw-documents",
        "description": "Uploads receipts, screenshots, and PDFs, and builds the evidence PDF.",
    },
    "complaint_selector": {
        "label": "Complaint selector",
        "kind": "section",
        "anchor": "cw-complaint-selector",
        "description": "Selects an existing complaint or starts a new one; also contains permanent deletion.",
    },
    "case_overview": {
        "label": "Case overview",
        "kind": "section",
        "anchor": "cw-case-overview",
        "description": "Shows the primary goal, current status, and conclusion for the selected complaint.",
    },
    "business_contacts": {
        "label": "Business contacts",
        "kind": "section",
        "anchor": "cw-business-contacts",
        "description": "Reviews or saves the exact company email, phone number, and website.",
    },
    "module_status": {
        "label": "Module status",
        "kind": "section",
        "anchor": "cw-module-status",
        "description": "Marks resolution, chargeback, social sharing, court filing, or regulatory escalation milestones.",
    },
    "resolution_strategy": {
        "label": "Resolution strategy",
        "kind": "section",
        "anchor": "cw-resolution-strategy",
        "description": "Shows the goal, fallbacks, escalation conditions, evidence needs, and communication policy.",
    },
    "recommendation": {
        "label": "Next recommended action",
        "kind": "section",
        "anchor": "cw-recommendation",
        "description": "Evaluates the selected case and proposes the next resolution step.",
    },
    "negotiation": {
        "label": "Negotiation and email",
        "kind": "section",
        "anchor": "cw-negotiation",
        "description": "Drafts the next message, previews the recipient, and sends selected drafts.",
    },
    "phone": {
        "label": "Phone escalation",
        "kind": "section",
        "anchor": "cw-phone",
        "description": "Checks whether a phone follow-up is due and places a call to the stored/discovered business number.",
    },
    "activity": {
        "label": "Combined activity log",
        "kind": "section",
        "anchor": "cw-activity",
        "description": "Shows email, phone, status, document, and system events for the selected complaint.",
    },
    "processor_log": {
        "label": "Decision / processor log",
        "kind": "section",
        "anchor": "cw-processor-log",
        "description": "Shows recent workflow and processing diagnostics.",
    },
    "gmail": {
        "label": "Gmail connection",
        "kind": "section",
        "anchor": "cw-gmail",
        "description": "Connects Gmail and selects the connected Gmail identity.",
    },
    "execution_mode": {
        "label": "Execution mode",
        "kind": "section",
        "anchor": "cw-execution-mode",
        "description": "Switches manual/automated mode, trusted mode, and runs one automated step.",
    },
    "chargeback": {
        "label": "Charge Back Initiator",
        "kind": "external",
        "env": "CW_CHARGE_BACK_APP_URL",
        "default_url": "/charge_back_initiator/",
        "description": "Opens the chargeback workflow after the merchant dispute is ready for bank escalation.",
    },
    "regulatory": {
        "label": "CW Regulatory",
        "kind": "external",
        "env": "CW_REGULATORY_APP_URL",
        "default_url": "/cw_regulatory/",
        "description": "Opens the regulator and government-agency complaint workflow.",
    },
    "small_claims": {
        "label": "Small Claim Court Warrior",
        "kind": "external",
        "env": "CW_SMALL_CLAIMS_APP_URL",
        "default_url": "/small_claim_court_warrior/",
        "description": "Opens the California small-claims packet and e-filing workflow.",
    },
    "social_share": {
        "label": "Social Network Poster",
        "kind": "external",
        "env": "CW_SOCIAL_SHARE_URL",
        "default_url": "",
        "description": "Opens the public social-network escalation workflow when configured.",
    },
}

ASSISTANT_CONTROL_GUIDE = [
    {"target": key, "control": value["label"], "what_it_does": value["description"]}
    for key, value in ASSISTANT_NAVIGATION.items()
]


def _assistant_chat_store() -> dict:
    if "case_assistant_chats" not in st.session_state:
        st.session_state.case_assistant_chats = {}
    return st.session_state.case_assistant_chats


def _assistant_history() -> list:
    user_email = (
        getattr(st.session_state.get("manager"), "user_email", "")
        or st.session_state.get("app_user_email")
        or "anonymous"
    ).strip().lower()
    chats = _assistant_chat_store()
    return chats.setdefault(user_email, [])


def _assistant_complaint_label(complaint_id: str) -> str:
    try:
        cs = st.session_state.manager.get_complaint(complaint_id)
        title = getattr(cs, "subject", "") or "Untitled complaint"
        return f"{cs.complaint_id} — {title}"
    except Exception:
        return ""


def _apply_assistant_navigation(action: dict) -> None:
    target = (action or {}).get("target") or ""
    complaint_id = (action or {}).get("complaint_id") or ""
    if target not in ASSISTANT_NAVIGATION:
        return

    if target == "new_complaint":
        _initialize_new_complaint_form()
    elif complaint_id:
        try:
            cs = st.session_state.manager.get_complaint(complaint_id)
            st.session_state.selected_complaint_id = complaint_id
            st.session_state.selected_thread_id = next(iter(cs.threads.keys()), None)
            st.session_state.initial_demand_selection_seen = None
            label = _assistant_complaint_label(complaint_id)
            if label:
                st.session_state.complaint_selector_label = label
        except Exception:
            complaint_id = ""

    st.session_state.case_assistant_pending_scroll = target
    st.session_state.case_assistant_nav_notice = (
        (action or {}).get("reason")
        or f"The assistant directed you to {ASSISTANT_NAVIGATION[target]['label']}."
    )


def _section_anchor(target: str) -> None:
    config = ASSISTANT_NAVIGATION.get(target) or {}
    anchor = config.get("anchor")
    if not anchor:
        return
    st.markdown(f'<div id="{anchor}"></div>', unsafe_allow_html=True)
    if st.session_state.get("case_assistant_pending_scroll") != target:
        return
    notice = st.session_state.get("case_assistant_nav_notice") or "Assistant navigation"
    st.info(f"**Assistant navigation:** {notice}")
    anchor_json = json.dumps(anchor)
    components.html(
        f"""
        <script>
        (function() {{
          const anchorId = {anchor_json};
          function navigate() {{
            try {{
              const element = window.parent.document.getElementById(anchorId);
              if (element) {{
                element.scrollIntoView({{behavior: 'smooth', block: 'start'}});
              }}
            }} catch (error) {{}}
          }}
          setTimeout(navigate, 120);
          setTimeout(navigate, 650);
        }})();
        </script>
        """,
        height=0,
        width=0,
    )
    st.session_state.case_assistant_pending_scroll = None
    st.session_state.case_assistant_nav_notice = ""


def _render_assistant_action(action: dict, message_index: int, action_index: int) -> None:
    target = (action or {}).get("target") or ""
    config = ASSISTANT_NAVIGATION.get(target)
    if not config:
        return
    label = (action or {}).get("label") or config["label"]
    complaint_id = (action or {}).get("complaint_id") or ""
    if complaint_id:
        label = f"{label} · {complaint_id}"
    reason = (action or {}).get("reason") or config.get("description", "")
    if reason:
        st.caption(reason)

    if config.get("kind") == "external":
        url = os.environ.get(config.get("env", ""), config.get("default_url", ""))
        if url:
            st.link_button(label, url, use_container_width=True)
        else:
            st.button(
                label,
                disabled=True,
                help=f"Set {config.get('env')} to enable this module link.",
                key=f"assistant_disabled_{message_index}_{action_index}_{target}",
                use_container_width=True,
            )
        return

    if st.button(
        label,
        key=f"assistant_nav_{message_index}_{action_index}_{target}_{complaint_id or 'none'}",
        use_container_width=True,
    ):
        _apply_assistant_navigation(action)
        st.rerun()


def _render_case_assistant() -> None:
    manager = st.session_state.manager
    config = manager.case_assistant_config()
    with st.expander("💬 Ask Complaint Warrior Assistant", expanded=True):
        st.caption(
            "Ask about any complaint belonging to the active user, compare cases, or ask where to click. "
            "The assistant can select a case and take you to the relevant control, but it cannot execute irreversible actions."
        )
        if not config.get("configured"):
            st.warning(
                "ChatGPT is not configured on this server. Set `OPENAI_API_KEY` or "
                "`CW_OPENAI_API_KEY`, then restart the app."
            )
        else:
            st.caption(f"Model: `{config.get('model')}` · Case details in each question are sent to the OpenAI API.")

        history = _assistant_history()
        if not history:
            with st.chat_message("assistant"):
                st.markdown(
                    "I can summarize all of your cases, identify which complaint needs attention, "
                    "explain any control, and take you to the relevant section."
                )

        for message_index, message in enumerate(history[-14:]):
            role = message.get("role") if message.get("role") in {"user", "assistant"} else "assistant"
            with st.chat_message(role):
                st.markdown(message.get("content") or "")
                cited = message.get("cited_complaint_ids") or []
                if cited:
                    st.caption("Cases used: " + ", ".join(cited))
                if message.get("requires_human_review"):
                    st.warning("Review this recommendation before taking a legal, financial, or irreversible action.")
                actions = message.get("actions") or []
                if actions:
                    st.markdown("**Go to the relevant place:**")
                    action_columns = st.columns(min(2, len(actions)))
                    for action_index, action in enumerate(actions):
                        with action_columns[action_index % len(action_columns)]:
                            _render_assistant_action(action, message_index, action_index)

        clear_col, _ = st.columns([1, 4])
        with clear_col:
            if st.button("Clear chat", use_container_width=True, key="clear_case_assistant_chat"):
                history.clear()
                st.rerun()

        with st.form("case_assistant_question_form", clear_on_submit=True):
            question = st.text_input(
                "Question",
                placeholder=(
                    "Examples: Which cases need action? What happened with the Ross complaint? "
                    "Take me to phone escalation for CMP-..."
                ),
                disabled=not config.get("configured"),
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button(
                "Ask ChatGPT",
                type="primary",
                disabled=not config.get("configured"),
            )

        if submitted and question.strip():
            history.append({"role": "user", "content": question.strip()})
            try:
                with st.spinner("Reviewing the active user's cases and interface controls..."):
                    result = manager.ask_case_assistant(
                        question.strip(),
                        control_guide=ASSISTANT_CONTROL_GUIDE,
                        allowed_navigation_targets=list(ASSISTANT_NAVIGATION.keys()),
                        selected_complaint_id=st.session_state.get("selected_complaint_id"),
                        conversation_history=[
                            {"role": item.get("role", ""), "content": item.get("content", "")}
                            for item in history[:-1]
                        ],
                    )
                assistant_message = {
                    "role": "assistant",
                    "content": result.get("answer") or "No answer was returned.",
                    "cited_complaint_ids": result.get("cited_complaint_ids") or [],
                    "actions": result.get("actions") or [],
                    "requires_human_review": bool(result.get("requires_human_review")),
                    "model": result.get("model"),
                    "request_id": result.get("request_id"),
                }
                history.append(assistant_message)
                auto_actions = [a for a in assistant_message["actions"] if a.get("auto_navigate")]
                if auto_actions:
                    _apply_assistant_navigation(auto_actions[0])
                st.rerun()
            except Exception as exc:
                _log(f"[case_assistant] request failed: {exc}")
                history.append({
                    "role": "assistant",
                    "content": (
                        "I could not complete the case analysis. Please try the question once more. "
                        "The technical details were added to the Decision / processor log."
                    ),
                    "actions": [],
                    "cited_complaint_ids": [],
                    "requires_human_review": True,
                })
                st.rerun()


def _log(msg: str):
    """Append only useful workflow messages to the visible Decision / processor log.

    Infrastructure messages such as Gmail token switching and active-user loading
    are useful for debugging server logs, but they clutter the user-facing CW log.
    """
    msg = str(msg or "").strip()
    if not msg:
        return

    noisy_prefixes = (
        "[manager] switched gmail token key",
        "[manager] active app user=",
        "[manager] complaints_loaded=",
        "[gmail]",
        "[token]",
        "[oauth]",
    )
    if any(msg.startswith(prefix) for prefix in noisy_prefixes):
        return

    noisy_contains = (
        "switched gmail token key",
        "complaints_loaded=",
    )
    if any(fragment in msg for fragment in noisy_contains):
        return

    if "logs" not in st.session_state:
        st.session_state.logs = []
    st.session_state.logs.append(msg)
    st.session_state.logs = st.session_state.logs[-500:]

def _init_state():
    if "logs" not in st.session_state:
        st.session_state.logs = []
    if "manager" not in st.session_state:
        st.session_state.manager = None
    if "delete_complaint_candidate" not in st.session_state:
        st.session_state.delete_complaint_candidate = None
    if "complaint_flash" not in st.session_state:
        st.session_state.complaint_flash = ""
    if "complaint_error" not in st.session_state:
        st.session_state.complaint_error = ""
    if "initial_demand_mode" not in st.session_state:
        st.session_state.initial_demand_mode = ""
    if "initial_demand_selection_seen" not in st.session_state:
        st.session_state.initial_demand_selection_seen = None
    if "initial_demand_loaded_complaint_id" not in st.session_state:
        st.session_state.initial_demand_loaded_complaint_id = None
    if "case_assistant_chats" not in st.session_state:
        st.session_state.case_assistant_chats = {}
    if "case_assistant_pending_scroll" not in st.session_state:
        st.session_state.case_assistant_pending_scroll = None
    if "case_assistant_nav_notice" not in st.session_state:
        st.session_state.case_assistant_nav_notice = ""

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

SUBSCRIPTION_DB = os.environ.get(
    "CW_SUBSCRIPTIONS_DB",
    os.environ.get("CW_COMPANIES_DB", "cw_companies.sqlite"),
)

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


def _selected_contact_keys(complaint_id: str) -> dict:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", complaint_id or "none")
    return {
        "email": f"selected_company_email_{safe}",
        "phone": f"selected_company_phone_{safe}",
        "website": f"selected_company_website_{safe}",
        "snapshot": f"selected_company_contact_snapshot_{safe}",
    }


def _request_selected_contact_refresh(complaint_id: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", complaint_id or "none")
    st.session_state[f"selected_company_contact_refresh_{safe}"] = True


def _sync_selected_contact_fields(cs, force: bool = False) -> None:
    keys = _selected_contact_keys(cs.complaint_id)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", cs.complaint_id or "none")
    refresh_key = f"selected_company_contact_refresh_{safe}"
    refresh_requested = bool(st.session_state.pop(refresh_key, False))
    snapshot = (
        getattr(cs, "company_email", "") or "",
        getattr(cs, "company_phone", "") or "",
        getattr(cs, "company_website", "") or "",
    )
    if force or refresh_requested or st.session_state.get(keys["snapshot"]) != snapshot:
        st.session_state[keys["email"]] = snapshot[0]
        st.session_state[keys["phone"]] = snapshot[1]
        st.session_state[keys["website"]] = snapshot[2]
        st.session_state[keys["snapshot"]] = snapshot



NEW_COMPLAINT_LABEL = "➕ New complaint"

INITIAL_DEMAND_KEYS = {
    "user_name": "initial_demand_user_name",
    "company_name": "initial_demand_company_name",
    "company_email": "initial_demand_company_email",
    "company_phone": "initial_demand_company_phone",
    "company_website": "initial_demand_company_website",
    "subject": "initial_demand_subject",
    "complaint_raw": "initial_demand_complaint_raw",
    "auto_send_policy": "initial_demand_auto_send_policy",
    "snapshot": "initial_demand_snapshot",
}


def _new_complaint_policy_default() -> str:
    return "auto_send" if (
        st.session_state.get("mode") == "automated"
        and st.session_state.get("trusted")
    ) else "manual"


def _initialize_new_complaint_form() -> None:
    """Clear section 1 and put it into new-complaint mode."""
    current_user_name = (
        st.session_state.get(INITIAL_DEMAND_KEYS["user_name"])
        or "Boris Galitsky"
    )
    st.session_state.initial_demand_mode = "new"
    st.session_state.initial_demand_loaded_complaint_id = None
    st.session_state.initial_demand_selection_seen = None
    st.session_state.selected_complaint_id = None
    st.session_state.selected_thread_id = None
    st.session_state.complaint_selector_label = NEW_COMPLAINT_LABEL
    st.session_state[INITIAL_DEMAND_KEYS["user_name"]] = current_user_name
    st.session_state[INITIAL_DEMAND_KEYS["company_name"]] = ""
    st.session_state[INITIAL_DEMAND_KEYS["company_email"]] = ""
    st.session_state[INITIAL_DEMAND_KEYS["company_phone"]] = ""
    st.session_state[INITIAL_DEMAND_KEYS["company_website"]] = ""
    st.session_state[INITIAL_DEMAND_KEYS["subject"]] = "Edit: Complaint regarding ..."
    st.session_state[INITIAL_DEMAND_KEYS["complaint_raw"]] = (
        "Specify your complaint so that we can find the contacts for the company"
    )
    st.session_state[INITIAL_DEMAND_KEYS["auto_send_policy"]] = (
        _new_complaint_policy_default()
    )
    st.session_state[INITIAL_DEMAND_KEYS["snapshot"]] = None


def _complaint_form_snapshot(cs) -> tuple:
    return (
        getattr(cs, "user_name", "") or "",
        getattr(cs, "company_name", "") or "",
        getattr(cs, "company_email", "") or "",
        getattr(cs, "company_phone", "") or "",
        getattr(cs, "company_website", "") or "",
        getattr(cs, "subject", "") or "",
        getattr(cs, "complaint_raw", "") or "",
        getattr(cs, "auto_send_policy", "manual") or "manual",
    )


def _load_complaint_into_initial_form(cs) -> None:
    """Load an existing complaint into section 1 for review."""
    snapshot = _complaint_form_snapshot(cs)
    st.session_state.initial_demand_mode = "existing"
    st.session_state.initial_demand_loaded_complaint_id = cs.complaint_id
    st.session_state.initial_demand_selection_seen = cs.complaint_id
    st.session_state[INITIAL_DEMAND_KEYS["user_name"]] = snapshot[0]
    st.session_state[INITIAL_DEMAND_KEYS["company_name"]] = snapshot[1]
    st.session_state[INITIAL_DEMAND_KEYS["company_email"]] = snapshot[2]
    st.session_state[INITIAL_DEMAND_KEYS["company_phone"]] = snapshot[3]
    st.session_state[INITIAL_DEMAND_KEYS["company_website"]] = snapshot[4]
    st.session_state[INITIAL_DEMAND_KEYS["subject"]] = snapshot[5]
    st.session_state[INITIAL_DEMAND_KEYS["complaint_raw"]] = snapshot[6]
    policy = snapshot[7] if snapshot[7] in AUTO_SEND_POLICIES else "manual"
    st.session_state[INITIAL_DEMAND_KEYS["auto_send_policy"]] = policy
    st.session_state[INITIAL_DEMAND_KEYS["snapshot"]] = snapshot


def _request_initial_demand_refresh(complaint_id: str) -> None:
    st.session_state[f"initial_demand_refresh_{complaint_id}"] = True


def _sync_initial_demand_form(manager, selected_complaint_id: Optional[str]) -> None:
    """Keep section 1 synchronized with the complaint dropdown.

    Selecting a different complaint loads it. Clicking Create new complaint
    intentionally keeps a blank form even though the old complaint remains
    selected on the right, until another complaint is selected.
    """
    if not selected_complaint_id:
        if st.session_state.get("initial_demand_mode") != "new":
            _initialize_new_complaint_form()
        return

    selection_changed = (
        st.session_state.get("initial_demand_selection_seen")
        != selected_complaint_id
    )
    if selection_changed:
        _load_complaint_into_initial_form(
            manager.get_complaint(selected_complaint_id)
        )
        return

    if (
        st.session_state.get("initial_demand_mode") == "existing"
        and st.session_state.get("initial_demand_loaded_complaint_id")
        == selected_complaint_id
    ):
        cs = manager.get_complaint(selected_complaint_id)
        snapshot = _complaint_form_snapshot(cs)
        refresh_key = f"initial_demand_refresh_{selected_complaint_id}"
        refresh_requested = bool(st.session_state.pop(refresh_key, False))
        if (
            refresh_requested
            or st.session_state.get(INITIAL_DEMAND_KEYS["snapshot"])
            != snapshot
        ):
            _load_complaint_into_initial_form(cs)


def _on_complaint_selector_change(label_to_id: dict[str, str]) -> None:
    label = st.session_state.get("complaint_selector_label")
    if label == NEW_COMPLAINT_LABEL:
        _initialize_new_complaint_form()
        return
    complaint_id = label_to_id.get(label)
    if not complaint_id:
        return
    st.session_state.selected_complaint_id = complaint_id
    try:
        cs = st.session_state.manager.get_complaint(complaint_id)
        st.session_state.selected_thread_id = next(iter(cs.threads.keys()), None)
    except Exception:
        st.session_state.selected_thread_id = None


def _add_complaint_from_initial_form() -> None:
    """Create a complaint from widget state inside a Streamlit callback.

    Streamlit executes callbacks before the script reruns, so this is the safe
    place to replace widget-backed session-state values with the newly loaded
    complaint. Mutating those keys in the ordinary button body raises
    StreamlitAPIException after the widgets have been instantiated.
    """
    try:
        manager = st.session_state.manager
        company_name = (
            st.session_state.get(INITIAL_DEMAND_KEYS["company_name"], "") or ""
        ).strip()
        company_email = (
            st.session_state.get(INITIAL_DEMAND_KEYS["company_email"], "") or ""
        ).strip().lower()
        company_phone = (
            st.session_state.get(INITIAL_DEMAND_KEYS["company_phone"], "") or ""
        ).strip()
        company_website = (
            st.session_state.get(INITIAL_DEMAND_KEYS["company_website"], "") or ""
        ).strip()
        subject = (
            st.session_state.get(INITIAL_DEMAND_KEYS["subject"], "") or ""
        ).strip()
        complaint_raw = (
            st.session_state.get(INITIAL_DEMAND_KEYS["complaint_raw"], "") or ""
        ).strip()
        user_name = (
            st.session_state.get(INITIAL_DEMAND_KEYS["user_name"], "") or ""
        ).strip()
        auto_policy = st.session_state.get(
            INITIAL_DEMAND_KEYS["auto_send_policy"], "manual"
        )

        subscribed = _is_company_subscribed(company_name)
        cs = manager.add_complaint(
            subject=subject,
            complaint_raw=complaint_raw,
            user_email=st.session_state.app_user_email,
            user_name=user_name,
            auto_send_policy=auto_policy,
            company_name=company_name,
            company_email=company_email,
            company_phone=company_phone,
            company_website=company_website,
        )

        st.session_state.selected_complaint_id = cs.complaint_id
        tid = next(iter(cs.threads.keys()))
        st.session_state.selected_thread_id = tid

        if subscribed:
            manager.draft_reply_now(cs.complaint_id, tid)
            manager.send_selected_drafts(
                cs.complaint_id, tid, [0], attachments=cs.docs
            )
            cs.current_status_summary = (
                "expecting the settlement from the company. Wait for their response"
            )
            cs.final_conclusion = "Awaiting company response"
            try:
                ts = cs.threads[tid]
                ts.status = "awaiting_company_response"
                ts.stage = "waiting_for_settlement"
            except Exception:
                pass
            try:
                cs.activities.append({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "channel": "system",
                    "kind": "status",
                    "title": "Subscribed company flow activated",
                    "detail": (
                        "First complaint email sent. Waiting for settlement "
                        "response from company."
                    ),
                    "meta": {
                        "company_name": company_name,
                        "subscription_db": SUBSCRIPTION_DB,
                    },
                })
            except Exception:
                pass
            st.session_state.complaint_flash = (
                f"Created complaint {cs.complaint_id}. Company is subscribed: "
                "first email sent, now waiting for response."
            )
        else:
            st.session_state.complaint_flash = (
                f"Created complaint {cs.complaint_id}."
            )

        # Safe here: callback runs before widgets are reconstructed on rerun.
        _load_complaint_into_initial_form(cs)
        st.session_state.complaint_selector_label = (
            f"{cs.complaint_id} — "
            f"{getattr(cs, 'subject', '') or 'Untitled complaint'}"
        )
        st.session_state.complaint_error = ""
    except Exception as exc:
        st.session_state.complaint_error = str(exc)


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
        _section_anchor("gmail")
        st.header("Gmail")
        st.info(outbound_email_mode_label())
        st.caption(outbound_email_mode_label())
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
                st.session_state.selected_complaint_id = None
                st.session_state.selected_thread_id = None
                st.session_state.initial_demand_selection_seen = None
                st.session_state.initial_demand_mode = ""
                st.session_state.case_assistant_pending_scroll = None
                st.session_state.case_assistant_nav_notice = ""
                st.session_state.pop("complaint_selector_label", None)
                st.success(f"Active user: {st.session_state.app_user_email}")
            except Exception as e:
                st.error(str(e))

        st.divider()
        _section_anchor("execution_mode")
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

    if st.session_state.complaint_flash:
        st.success(st.session_state.complaint_flash)
        st.session_state.complaint_flash = ""
    if st.session_state.complaint_error:
        st.error(st.session_state.complaint_error)
        st.session_state.complaint_error = ""

    _render_case_assistant()

    # Resolve the selected complaint before rendering the left pane. The
    # complaint selectbox callback updates selected_complaint_id before rerun,
    # so section 1 immediately loads the newly selected complaint.
    complaints = st.session_state.manager.list_complaints()
    complaint_by_id = {c.complaint_id: c for c in complaints}
    selected_id = st.session_state.get("selected_complaint_id")
    if complaints and selected_id not in complaint_by_id:
        if st.session_state.get("initial_demand_mode") != "new":
            selected = complaints[0]
            st.session_state.selected_complaint_id = selected.complaint_id
            st.session_state.selected_thread_id = next(
                iter(selected.threads.keys()), None
            )
    elif not complaints:
        st.session_state.selected_complaint_id = None
        st.session_state.selected_thread_id = None

    _sync_initial_demand_form(
        st.session_state.manager,
        st.session_state.get("selected_complaint_id"),
    )

    # Left: complaint form / attachments
    left, right = st.columns([1, 2], gap="large")

    with left:
        _section_anchor("initial_demand")
        st.subheader("1) Initial demand")
        st.markdown("### Quick Actions")
        st.markdown(f"- [Charge Back Initiator]({os.environ.get('CW_CHARGE_BACK_APP_URL', '/charge_back_initiator/')})")
        st.markdown(f"- [CW Regulatory]({os.environ.get('CW_REGULATORY_APP_URL', '/cw_regulatory/')})")
        st.markdown(f"- [Small Claim Court Warrior]({os.environ.get('CW_SMALL_CLAIMS_APP_URL', '/small_claim_court_warrior/')})")
        social_url = os.environ.get("CW_SOCIAL_SHARE_URL", "")
        if social_url:
            st.markdown(f"- [Social Network Poster]({social_url})")

        form_mode = st.session_state.get("initial_demand_mode") or "new"
        creating_new = form_mode == "new"
        loaded_id = st.session_state.get("initial_demand_loaded_complaint_id")
        if creating_new:
            st.info("New complaint form. Complete the fields and click **Add complaint**.")
        else:
            st.info(
                f"Loaded complaint **{loaded_id}**. Click **Create new complaint** "
                "to clear and initialize the form for another complaint."
            )

        your_name = st.text_input(
            "Your name",
            key=INITIAL_DEMAND_KEYS["user_name"],
            disabled=not creating_new,
        )
        company_name = st.text_input(
            "Company name",
            key=INITIAL_DEMAND_KEYS["company_name"],
            disabled=not creating_new,
        )
        company_email = st.text_input(
            "Company / business email",
            key=INITIAL_DEMAND_KEYS["company_email"],
            disabled=not creating_new,
            help=(
                "Optional. A value entered here is authoritative and will not be replaced. "
                "If blank, Draft next message can discover and save an email."
            ),
        ).strip().lower()
        company_phone = st.text_input(
            "Company phone",
            key=INITIAL_DEMAND_KEYS["company_phone"],
            disabled=not creating_new,
            help=(
                "Optional. A value entered here is authoritative and is the exact number "
                "supplied to the call agent. Discovery runs only when this field is blank."
            ),
        ).strip()
        company_website = st.text_input(
            "Company website",
            key=INITIAL_DEMAND_KEYS["company_website"],
            disabled=not creating_new,
            help=(
                "Optional. A value entered here is authoritative. When email or phone is "
                "missing, this website can be checked for public business contacts."
            ),
        ).strip()
        subject = st.text_input(
            "Subject",
            key=INITIAL_DEMAND_KEYS["subject"],
            disabled=not creating_new,
        )
        raw = st.text_area(
            "Describe the complaint",
            height=180,
            key=INITIAL_DEMAND_KEYS["complaint_raw"],
            disabled=not creating_new,
        )
        auto_policy = st.selectbox(
            "Communication policy",
            list(AUTO_SEND_POLICIES),
            key=INITIAL_DEMAND_KEYS["auto_send_policy"],
            disabled=not creating_new,
        )

        new_col, add_col = st.columns(2)
        with new_col:
            st.button(
                "Create new complaint",
                use_container_width=True,
                help="Clear and initialize section 1 for a new complaint.",
                on_click=_initialize_new_complaint_form,
            )

        with add_col:
            st.button(
                "Add complaint",
                type="primary",
                use_container_width=True,
                disabled=not creating_new,
                help=(
                    "Create the complaint using the values currently entered in section 1."
                    if creating_new
                    else "Click Create new complaint before adding another complaint."
                ),
                on_click=_add_complaint_from_initial_form,
            )

        st.divider()
        _section_anchor("documents")
        st.subheader("Documents")
        document_complaint_id = (
            st.session_state.get("initial_demand_loaded_complaint_id")
            if st.session_state.get("initial_demand_mode") == "existing"
            else None
        )
        if document_complaint_id:
            files = st.file_uploader(
                "Upload receipts / screenshots / PDFs",
                accept_multiple_files=True,
                key=f"complaint_documents_{document_complaint_id}",
            )
            if files:
                saved = _save_uploads(files)
                st.session_state.manager.attach_docs(document_complaint_id, saved)
                st.success(f"Uploaded {len(saved)} file(s).")
            if st.button(
                "Build evidence PDF",
                key=f"build_evidence_{document_complaint_id}",
            ):
                out = str(UPLOAD_DIR / f"{document_complaint_id}_evidence.pdf")
                st.session_state.manager.build_evidence_pdf(
                    document_complaint_id, out
                )
                st.success("Evidence PDF generated.")
        elif creating_new:
            st.caption("Add the new complaint before attaching documents.")
        else:
            st.caption("Select a complaint first.")

    with right:
        _section_anchor("complaint_selector")
        st.subheader("2) Complaint")
        complaint_labels = [NEW_COMPLAINT_LABEL]
        complaint_id_by_label = {}

        for c in complaints:
            title = getattr(c, "subject", "") or getattr(c, "title", "") or "Untitled complaint"
            label = f"{c.complaint_id} — {title}"
            complaint_labels.append(label)
            complaint_id_by_label[label] = c.complaint_id

        current_label = NEW_COMPLAINT_LABEL
        for label, complaint_id in complaint_id_by_label.items():
            if complaint_id == st.session_state.selected_complaint_id:
                current_label = label
                break

        idx = complaint_labels.index(current_label) if current_label in complaint_labels else 0
        desired_label = complaint_labels[idx]
        if st.session_state.get("complaint_selector_label") not in complaint_labels:
            st.session_state.complaint_selector_label = desired_label
        elif complaint_id_by_label.get(st.session_state.complaint_selector_label) != st.session_state.get("selected_complaint_id"):
            st.session_state.complaint_selector_label = desired_label

        select_col, delete_col = st.columns([8, 1], vertical_alignment="bottom")
        with select_col:
            selected_label = st.selectbox(
                "Complaint",
                complaint_labels,
                key="complaint_selector_label",
                on_change=_on_complaint_selector_change,
                args=(complaint_id_by_label,),
            )

        cid = complaint_id_by_label.get(selected_label)
        st.session_state.selected_complaint_id = cid

        with delete_col:
            if cid and st.button(
                "🗑️ Delete",
                key=f"request_delete_complaint_{cid}",
                help="Permanently delete this complaint from Complaint Warrior.",
                use_container_width=True,
            ):
                st.session_state.delete_complaint_candidate = cid

        if not cid:
            st.info(
                "Section 1 is initialized for a new complaint. Complete it and "
                "click Add complaint, or select an existing complaint here."
            )
            st.stop()

        delete_candidate = st.session_state.delete_complaint_candidate
        if delete_candidate:
            candidate = next(
                (c for c in complaints if c.complaint_id == delete_candidate),
                None,
            )
            if candidate is None:
                st.session_state.delete_complaint_candidate = None
            else:
                candidate_title = (
                    getattr(candidate, "subject", "")
                    or getattr(candidate, "title", "")
                    or "Untitled complaint"
                )
                with st.container(border=True):
                    st.warning(
                        f"Permanently delete **{candidate_title}** "
                        f"(`{delete_candidate}`)? This cannot be undone."
                    )
                    st.caption(
                        "The complaint, stored phone-call results, and associated small-claims "
                        "packet/e-filing records will be deleted. Gmail messages and local "
                        "evidence/upload files will be retained."
                    )
                    confirm_delete = st.checkbox(
                        "I understand that this complaint will be permanently deleted.",
                        key=f"confirm_delete_complaint_{delete_candidate}",
                    )
                    confirm_col, cancel_col = st.columns(2)
                    with confirm_col:
                        if st.button(
                            "Delete permanently",
                            type="primary",
                            disabled=not confirm_delete,
                            key=f"confirm_delete_button_{delete_candidate}",
                            use_container_width=True,
                        ):
                            try:
                                result = st.session_state.manager.delete_complaint(delete_candidate)
                                remaining = st.session_state.manager.list_complaints()
                                if remaining:
                                    next_complaint = remaining[0]
                                    st.session_state.selected_complaint_id = next_complaint.complaint_id
                                    st.session_state.selected_thread_id = (
                                        next(iter(next_complaint.threads.keys()), None)
                                    )
                                else:
                                    st.session_state.selected_complaint_id = None
                                    st.session_state.selected_thread_id = None
                                st.session_state.delete_complaint_candidate = None
                                st.session_state.initial_demand_selection_seen = None
                                removed_calls = result.get("phone_results_deleted", 0)
                                suffix = (
                                    f" Associated phone records removed: {removed_calls}."
                                    if removed_calls
                                    else ""
                                )
                                st.session_state.complaint_flash = (
                                    f"Complaint {delete_candidate} was permanently deleted.{suffix}"
                                )
                                st.rerun()
                            except Exception as e:
                                st.error(f"Could not delete complaint: {e}")
                    with cancel_col:
                        if st.button(
                            "Cancel",
                            key=f"cancel_delete_complaint_{delete_candidate}",
                            use_container_width=True,
                        ):
                            st.session_state.delete_complaint_candidate = None
                            st.rerun()

        cs = st.session_state.manager.get_complaint(cid)

        # resolution strategy/status
        _section_anchor("case_overview")
        st.subheader("3) Resolution strategy and current status")
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

        _section_anchor("business_contacts")
        st.markdown("#### Business contact")
        try:
            _sync_selected_contact_fields(cs)
            contact_keys = _selected_contact_keys(cid)
            contact = st.session_state.manager.get_business_contact_summary(cid)
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                selected_company_email = st.text_input(
                    "Company / business email",
                    key=contact_keys["email"],
                    help="A user-entered value is authoritative. If blank, drafting/sending may discover and save an email.",
                ).strip().lower()
                if contact.get("email_source"):
                    st.caption(f"Source: {contact.get('email_source')}")
            with cc2:
                selected_company_phone = st.text_input(
                    "Company phone",
                    key=contact_keys["phone"],
                    help="This exact stored number is supplied to the phone agent. Discovery cannot replace a nonblank value.",
                ).strip()
                if contact.get("phone_source"):
                    st.caption(f"Source: {contact.get('phone_source')}")
            with cc3:
                selected_company_website = st.text_input(
                    "Company website",
                    key=contact_keys["website"],
                    help="A user-entered value is authoritative. A discovered website is shown here for review.",
                ).strip()
                if contact.get("website_source"):
                    st.caption(f"Source: {contact.get('website_source')}")

            if st.button("Save business contacts", key=f"save_business_contacts_{cid}"):
                saved_contact = st.session_state.manager.update_business_contacts(
                    cid,
                    email=selected_company_email,
                    phone=selected_company_phone,
                    website=selected_company_website,
                )
                cs = st.session_state.manager.get_complaint(cid)
                _request_selected_contact_refresh(cid)
                _request_initial_demand_refresh(cid)
                st.success(
                    "Business contacts saved. User-entered values are authoritative and will not be replaced by discovery."
                )
                st.rerun()
            if not any((contact.get("email"), contact.get("phone"), contact.get("website"))):
                st.caption("No business contact is stored yet. Drafting or calling will discover only the contact required for that action.")
        except Exception as e:
            st.caption(f"Business contact status unavailable: {e}")

        _section_anchor("module_status")
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

        _section_anchor("resolution_strategy")
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

        _section_anchor("recommendation")
        st.markdown("#### Overall check and next recommended action")
        _render_next_recommendation(st.session_state.manager, cid, tid)

        # clear sequencing
        _section_anchor("negotiation")
        st.subheader("4) Negotiation step")
        b1, b2 = st.columns([1, 1])

        with b1:
            if st.button("Draft next message", type="primary", disabled=actions_paused):
                try:
                    st.session_state.manager.draft_reply_now(cid, tid)
                    contact = st.session_state.manager.get_business_contact_summary(cid)
                    _request_selected_contact_refresh(cid)
                    _request_initial_demand_refresh(cid)
                    found = [x for x in [contact.get("email"), contact.get("phone"), contact.get("website")] if x]
                    suffix = f" Contact available: {' | '.join(found)}." if found else " No business contact was found yet."
                    st.success("Draft generated." + suffix)
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
                try:
                    route = st.session_state.manager.get_outbound_recipient_preview(cid, tid, selected_idx[0])
                    actual = route.get("actual_recipient") or "[blocked / missing]"
                    intended = route.get("intended_recipient") or "[not found]"
                    mode = route.get("deployment_mode") or "unknown"
                    if route.get("error"):
                        st.error(f"Email send target: {route.get('error')}")
                    elif route.get("test_redirect"):
                        st.warning(f"Email will be sent to {actual} (debug mode). Intended business email: {intended}.")
                    else:
                        st.success(f"Email will be sent to business recipient: {actual} (mode: {mode}).")
                except Exception as e:
                    st.warning(f"Could not preview email target: {e}")

        with b2:
            if st.button("Send selected drafts", disabled=actions_paused):
                try:
                    st.session_state.manager.send_selected_drafts(cid, tid, selected_idx or [0], attachments=cs.docs)
                    st.success("Sent.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        _section_anchor("phone")
        st.subheader("5) Phone contact")
        phone_button_label = "Call company now"
        try:
            phone_followup = st.session_state.manager.get_phone_followup_status(cid, tid, days_without_reply=3, max_attempts=2)
            if phone_followup.get("due"):
                phone_button_label = "Need to call now"
                st.warning(
                    f"No email reply for {phone_followup.get('days_without_reply', 0):.1f} days after the last sent email. "
                    f"Phone follow-up is due. Attempts used: {phone_followup.get('attempts', 0)}/2."
                )
            else:
                reason = phone_followup.get("reason") or ""
                if reason:
                    st.caption(reason)
        except Exception as e:
            st.caption(f"Phone follow-up check unavailable: {e}")

        try:
            current_call_contact = st.session_state.manager.get_business_contact_summary(cid)
            if current_call_contact.get("phone"):
                st.info(
                    f"Number to be called: **{current_call_contact.get('phone')}** "
                    f"(source: {current_call_contact.get('phone_source') or 'stored'})"
                )
            else:
                st.caption("No phone is stored. Clicking Call will try to discover one; the call is blocked if no exact number is found.")
        except Exception:
            pass

        if st.button(phone_button_label, disabled=actions_paused):
            try:
                reply = st.session_state.manager.place_phone_call_and_capture_reply(cid, tid)
                contact = st.session_state.manager.get_business_contact_summary(cid)
                _request_selected_contact_refresh(cid)
                _request_initial_demand_refresh(cid)
                if contact.get("phone"):
                    st.caption(f"Called number: {contact.get('phone')}")
                elif contact.get("email"):
                    st.caption(f"No phone was stored; lookup also used the business email/domain: {contact.get('email')}")
                if reply and reply.strip():
                    st.success("Phone reply captured and overall recommendation updated.")
                    st.text_area("Phone reply", reply, height=180)
                else:
                    st.warning("Call completed, but no phone reply transcript was captured. Overall recommendation was not escalated.")
                st.rerun()
            except Exception as e:
                st.error(f"Phone call failed: {e}")

        _section_anchor("activity")
        st.subheader("6) Combined activity log")
        for ev in reversed(cs.activities[-50:]):
            with st.container(border=True):
                st.markdown(f"**{ev['ts']}** — `{ev['channel']}` / `{ev['kind']}` — **{ev['title']}**")
                st.write(ev["detail"])
                if ev.get("meta"):
                    with st.expander("metadata"):
                        st.json(ev["meta"])

    st.divider()
    _section_anchor("processor_log")
    st.subheader("Decision / processor log")
    st.code("\n".join(st.session_state.logs[-250:]) if st.session_state.logs else "(no logs yet)", language="text")


if __name__ == "__main__":
    main()
