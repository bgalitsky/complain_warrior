# entry_frontend.py
# -*- coding: utf-8 -*-

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Tuple

import streamlit as st


APP_TITLE = "Complaint Warrior"
DB_PATH = os.environ.get("CW_SUBSCRIPTIONS_DB", "cw_companies.sqlite")


BASE_PUBLIC_URL = os.environ.get(
    "CW_PUBLIC_BASE_URL",
    "https://foresakenly-figgiest-jazmin.ngrok-free.dev"
).rstrip("/")

CUSTOMER_APP_URL = os.environ.get(
    "CW_CUSTOMER_APP_URL",
    f"{BASE_PUBLIC_URL}/complaint_warrior"
)
COMPANY_APP_URL = os.environ.get(
    "CW_COMPANY_APP_URL",
    f"{BASE_PUBLIC_URL}/complaint_warrior"
)
SMALL_CLAIMS_APP_URL = os.environ.get(
    "CW_SMALL_CLAIMS_APP_URL",
    f"{BASE_PUBLIC_URL}/small_claim_court_warrior"
)
LOGO_PATH = os.environ.get("CW_LOGO_PATH", "complaint_warrior.png")


# -----------------------------
# DB
# -----------------------------
def get_conn(db_path: str = DB_PATH):
    con = sqlite3.connect(db_path, check_same_thread=False)
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
    con.commit()
    return con


def normalize_company_name(name: str) -> str:
    return " ".join((name or "").strip().split())


def upsert_company_subscription(
    company_name: str,
    contact_name: str,
    contact_email: str,
    contact_phone: str,
    subscription_tier: str,
    notes: str,
    is_active: bool = True,
    db_path: str = DB_PATH,
) -> None:
    company_name = normalize_company_name(company_name)
    if not company_name:
        raise ValueError("Company name is required.")

    now = time.time()
    con = get_conn(db_path)
    cur = con.cursor()

    row = cur.execute(
        "SELECT id FROM subscribed_companies WHERE company_name = ?",
        (company_name,)
    ).fetchone()

    if row:
        cur.execute("""
            UPDATE subscribed_companies
            SET contact_name = ?,
                contact_email = ?,
                contact_phone = ?,
                subscription_tier = ?,
                notes = ?,
                is_active = ?,
                updated_at = ?
            WHERE company_name = ?
        """, (
            contact_name.strip(),
            contact_email.strip().lower(),
            contact_phone.strip(),
            subscription_tier.strip(),
            notes.strip(),
            1 if is_active else 0,
            now,
            company_name,
        ))
    else:
        cur.execute("""
            INSERT INTO subscribed_companies(
                company_name,
                contact_name,
                contact_email,
                contact_phone,
                subscription_tier,
                notes,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_name,
            contact_name.strip(),
            contact_email.strip().lower(),
            contact_phone.strip(),
            subscription_tier.strip(),
            notes.strip(),
            1 if is_active else 0,
            now,
            now,
        ))

    con.commit()
    con.close()


def is_company_subscribed(company_name: str, db_path: str = DB_PATH) -> bool:
    company_name = normalize_company_name(company_name)
    if not company_name:
        return False

    con = get_conn(db_path)
    row = con.execute("""
        SELECT is_active
        FROM subscribed_companies
        WHERE company_name = ?
    """, (company_name,)).fetchone()
    con.close()

    return bool(row and row[0] == 1)


def list_subscribed_companies(db_path: str = DB_PATH) -> List[Tuple]:
    con = get_conn(db_path)
    rows = con.execute("""
        SELECT company_name, contact_name, contact_email, contact_phone,
               subscription_tier, is_active, updated_at
        FROM subscribed_companies
        ORDER BY company_name COLLATE NOCASE
    """).fetchall()
    con.close()
    return rows


# -----------------------------
# UI helpers
# -----------------------------
def render_header():
    c1, c2 = st.columns([0.15, 0.85])
    with c1:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width=110)
    with c2:
        st.title(APP_TITLE)
        st.caption(
            "Customer complaint resolution through structured negotiation, persistent escalation, "
            "and optional company-side mediation."
        )


def render_home():
    st.subheader("Choose your path")

    col1, col2 = st.columns(2, gap="large")

    with col1:
        st.markdown("### For customers")
        st.write(
            "Complaint Warrior is free for customers and will remain free. "
            "We do not charge the customer for filing, organizing, and pursuing a complaint."
        )
        st.write(
            "You can either manage the complaint yourself or let Complaint Warrior handle the outreach, "
            "follow-up, and escalation process."
        )
        if st.button("I am a customer", use_container_width=True, type="primary"):
            st.session_state.entry_path = "customer"
            st.rerun()

    with col2:
        st.markdown("### For companies")
        st.write(
            "Companies can subscribe to Complaint Warrior so complaints are moderated toward a practical "
            "compromise between company and customer before they turn into repeated escalations."
        )
        st.write(
            "If a company does not subscribe, Complaint Warrior may continue persistent outbound contact "
            "by email and phone in order to secure a compensation outcome for the customer."
        )
        if st.button("I represent a company", use_container_width=True):
            st.session_state.entry_path = "company"
            st.rerun()

    st.divider()
    with st.container(border=True):
        st.markdown("### Small Claim Court Warrior")
        st.write(
            "If Complaint Warrior has exhausted negotiation and escalation options, you can continue to "
            "Small Claim Court Warrior to review complaints that are ready to proceed toward California small claims court."
        )
        st.markdown(f"[Open Small Claim Court Warrior]({SMALL_CLAIMS_APP_URL})")


def render_customer_path():
    st.subheader("Customer path")
    st.info(
        "Complaint Warrior is free for customers. We do not charge you. "
        "We do everything possible to persuade the company to provide a fair resolution."
    )

    st.write(
        "You have two options:"
    )

    a, b = st.columns(2, gap="large")

    with a:
        st.markdown("#### Handle it myself")
        st.write(
            "Use your own email, phone, or company portal. You stay fully in control of all communication."
        )
        company_name = st.text_input("Company name", key="cust_manual_company")
        issue = st.text_area("Short complaint summary", key="cust_manual_summary", height=140)

        if st.button("Prepare my manual complaint plan", key="manual_plan_btn", use_container_width=True):
            if not company_name.strip():
                st.warning("Please enter the company name.")
            else:
                subscribed = is_company_subscribed(company_name)
                if subscribed:
                    st.success(
                        f"{company_name.strip()} is subscribed. "
                        "This may improve the chance of faster mediated resolution."
                    )
                else:
                    st.warning(
                        f"{company_name.strip()} is not listed as subscribed. "
                        "You can still proceed manually, or let Complaint Warrior escalate for you."
                    )

                st.markdown("##### Suggested manual sequence")
                st.markdown(
                    "1. Send a concise written complaint.\n"
                    "2. Attach receipts, screenshots, and timelines.\n"
                    "3. Ask for a concrete remedy and deadline.\n"
                    "4. Escalate to supervisor or executive support if ignored.\n"
                    "5. Preserve all replies and call notes."
                )

    with b:
        st.markdown("#### Proceed with Complaint Warrior")
        st.write(
            "Start the complaint process in the Complaint Warrior app, where you can describe the issue, "
            "upload documents, and begin negotiation and escalation."
        )
        company_name2 = st.text_input("Company name ", key="cust_cw_company")
        issue2 = st.text_area("Complaint summary ", key="cust_cw_summary", height=140)

        if company_name2.strip():
            if is_company_subscribed(company_name2):
                st.success(
                    f"{company_name2.strip()} is subscribed. Complaint Warrior will aim first for mediated compromise."
                )
            else:
                st.warning(
                    f"{company_name2.strip()} is not subscribed. Complaint Warrior may use standard persistent escalation."
                )

        st.markdown(
            f"[Open Complaint Warrior customer workflow]({CUSTOMER_APP_URL})"
        )
        st.caption(
            f"If Complaint Warrior has already exhausted its options for this case, continue in "
            f"[Small Claim Court Warrior]({SMALL_CLAIMS_APP_URL})."
        )

    st.divider()
    if st.button("← Back to main page"):
        st.session_state.entry_path = None
        st.rerun()


def render_company_path():
    st.subheader("Company path")

    st.warning(
        "Subscription allows Complaint Warrior to moderate customer complaints toward a compromise solution "
        "before they become repeated outbound escalations."
    )

    st.write(
        "If your company subscribes, Complaint Warrior will attempt structured mediation: clarify the facts, "
        "test acceptable compromise options, and guide the parties toward resolution."
    )

    st.write(
        "If your company does not subscribe, Complaint Warrior will continue acting for the customer through "
        "persistent email and phone outreach, follow-up reminders, and escalation pressure aimed at securing compensation."
    )

    st.write(
        "In practice, this means your existing customer-service and CRM workflows may face more repeated complaint traffic "
        "from Complaint Warrior than they would under a subscribed mediation model."
    )

    st.markdown("### Subscribe your company")

    company_name = st.text_input("Company name")
    contact_name = st.text_input("Contact name")
    contact_email = st.text_input("Contact email")
    contact_phone = st.text_input("Contact phone")
    subscription_tier = st.selectbox(
        "Subscription tier",
        ["standard", "priority", "enterprise"],
        index=0
    )
    notes = st.text_area(
        "Notes",
        height=120,
        placeholder="Preferred handling process, escalation contact, service windows, etc."
    )

    if company_name.strip():
        if is_company_subscribed(company_name):
            st.success(f"{company_name.strip()} is already subscribed.")
        else:
            st.info(f"{company_name.strip()} is not currently subscribed.")

    c1, c2 = st.columns(2)

    with c1:
        if st.button("Subscribe company", type="primary", use_container_width=True):
            try:
                upsert_company_subscription(
                    company_name=company_name,
                    contact_name=contact_name,
                    contact_email=contact_email,
                    contact_phone=contact_phone,
                    subscription_tier=subscription_tier,
                    notes=notes,
                    is_active=True,
                )
                st.success(f"{company_name.strip()} has been subscribed.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    with c2:
        if st.button("Open company complaint console", use_container_width=True):
            st.markdown(f"[Continue to company workflow]({COMPANY_APP_URL})")

    st.divider()
    st.markdown("### Current subscribed companies")
    rows = list_subscribed_companies()
    if rows:
        for row in rows:
            cname, person, email, phone, tier, active, updated = row
            with st.container(border=True):
                st.markdown(f"**{cname}**")
                st.write(
                    {
                        "contact_name": person,
                        "contact_email": email,
                        "contact_phone": phone,
                        "tier": tier,
                        "active": bool(active),
                        "updated_at_epoch": updated,
                    }
                )
    else:
        st.caption("No subscribed companies yet.")

    st.divider()
    if st.button("← Back to main page"):
        st.session_state.entry_path = None
        st.rerun()


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")

    if "entry_path" not in st.session_state:
        st.session_state.entry_path = None

    render_header()

    with st.sidebar:
        st.header("Navigation")
        st.write(f"Subscription DB: `{DB_PATH}`")
        st.markdown(f"[Customer workflow]({CUSTOMER_APP_URL})")
        st.markdown(f"[Company workflow]({COMPANY_APP_URL})")
        st.markdown(f"[Small Claim Court Warrior]({SMALL_CLAIMS_APP_URL})")

    if st.session_state.entry_path is None:
        render_home()
    elif st.session_state.entry_path == "customer":
        render_customer_path()
    elif st.session_state.entry_path == "company":
        render_company_path()


if __name__ == "__main__":
    main()