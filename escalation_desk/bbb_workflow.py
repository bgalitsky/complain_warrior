"""Streamlit integration; Playwright runs locally, never in a Streamlit callback."""
import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing

INSTRUCTIONS = '''BBB guided submission
=====================
1. Extract this entire ZIP into a private folder on your computer.
2. Install Python 3.10+ if needed. Open a terminal in the extracted folder.
3. Run: python -m pip install -r requirements.txt
4. Run: python -m playwright install chromium
5. Run: python bbb_companion.py
   On Windows you can use py instead of python.
6. In Chromium start the complaint, log in manually, and select the correct business.
7. Return to the terminal. Choose the BBB tab, a visible field and a complaint value.
   Review each mapping before confirming. Repeat on subsequent pages.
8. Answer eligibility questions, upload evidence, navigate and submit in the browser yourself.
9. After BBB confirms submission, copy the reference number into Complaint Warrior.

The helper never clicks Next, Submit or declarations and never handles passwords/CAPTCHA.
It reads only visible supported controls on HTTPS bbb.org pages. Custom widgets and
third-party frames must be filled manually. Missing data is never invented.
Authentication uses a temporary profile that is removed on normal exit. Closing the helper
may lose unsaved form progress. An interrupted run is not evidence of submission failure:
check BBB before retrying. BBB may autosave before final submission.
This package contains personal complaint data: keep it private and delete it when done.
Evidence files are not bundled: choose the intended files manually in BBB.
'''


def build_package(complaint_id, values):
    payload = {'version': 1, 'complaint_id': str(complaint_id), 'values': values}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('complaint.json', json.dumps(payload, ensure_ascii=False, indent=2))
        archive.write(Path(__file__).with_name('bbb_companion.py'), 'bbb_companion.py')
        archive.writestr('requirements.txt', 'playwright>=1.45,<2\n')
        archive.writestr('README.txt', INSTRUCTIONS)
    return stream.getvalue()


def record_submission(db_path, complaint_id, user_email, reference):
    """An explicitly user-reported outcome, not browser-verified submission."""
    reference = reference.strip()
    if not reference or len(reference) > 200 or not user_email.strip():
        raise ValueError('Enter a BBB reference (up to 200 characters) and a consumer email.')
    with closing(sqlite3.connect(db_path)) as con, con:
        con.execute('''CREATE TABLE IF NOT EXISTS cw_bbb_submissions (
            complaint_id TEXT NOT NULL, user_email TEXT NOT NULL,
            reference TEXT NOT NULL, recorded_at TEXT NOT NULL,
            verification TEXT NOT NULL,
            PRIMARY KEY (complaint_id, user_email)
        )''')
        con.execute('''INSERT INTO cw_bbb_submissions VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(complaint_id, user_email) DO NOTHING''',
            (str(complaint_id), user_email.strip().lower(), reference,
             datetime.now(timezone.utc).isoformat(), 'user_reported'))


def existing_submission(db_path, complaint_id, user_email):
    with closing(sqlite3.connect(db_path)) as con, con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cw_bbb_submissions'").fetchone()
        if not exists:
            return None
        return con.execute('SELECT reference, recorded_at FROM cw_bbb_submissions WHERE complaint_id=? AND user_email=?',
                           (str(complaint_id), user_email.strip().lower())).fetchone()


def submission_block_reason(raw):
    statuses = (raw or {}).get('module_statuses') or {}
    if not isinstance(statuses, dict):
        return ''
    for key, message in (
        ('submitted_to_small_claim_court', 'This complaint is marked as submitted to court. Check BBB eligibility before further action.'),
        ('resolved', 'This complaint is marked resolved.'),
        ('escalated_to_authorities', 'This complaint is already escalated. Wait for the existing process before starting another escalation.'),
    ):
        entry = statuses.get(key)
        done = entry.get('done', False) if isinstance(entry, dict) else entry
        if done:
            return message
    return ''


def render_bbb_panel(record, db_path):
    import streamlit as st
    prefix = 'bbb_' + hashlib.sha256((db_path + str(record.complaint_id) + record.user_email).encode()).hexdigest()[:20]
    with st.expander('BBB: fill a complaint with a local browser helper'):
        st.write('Download the helper, log into BBB in its browser, confirm the business, and fill the form one field at a time. You review and submit on BBB.')
        st.caption('Works with hosted Complaint Warrior: the browser runs on your computer. Python is required locally. BBB is a dispute-resolution organization, not a government regulator.')
        saved = existing_submission(db_path, record.complaint_id, record.user_email)
        if saved:
            st.success(f'User-reported BBB submission: {saved[0]} (recorded {saved[1]}).')
            st.info('Check your existing BBB case before creating another submission.')
            return
        blocked = submission_block_reason(record.raw_json)
        if blocked:
            st.warning(blocked)
        st.link_button('Open BBB complaint page', 'https://www.bbb.org/file-a-complaint')
        values = {}
        for key, label, default in (
            ('full_name', 'Consumer full name', record.user_name),
            ('email', 'Consumer email', record.user_email),
            ('business_name', 'Business name', record.company_name),
            ('business_address', 'Business address (confirm the correct location)', ''),
            ('postal_address', 'Consumer postal address', ''),
            ('phone', 'Consumer phone', ''),
            ('transaction_date', 'Transaction date (YYYY-MM-DD)', ''),
            ('amount', 'Disputed amount', ''),
        ):
            values[key] = st.text_input(label, value=default or '', key=prefix + key)
        values['narrative'] = st.text_area('BBB narrative — review facts and remove private information not needed in the narrative',
                                          value=record.display_text(), height=200, key=prefix + 'narrative')
        values['requested_resolution'] = st.text_area('Requested resolution', key=prefix + 'resolution')
        approved = st.checkbox('I reviewed this information and want to use it to fill my BBB complaint.', key=prefix + 'approved')
        st.download_button('Download BBB browser helper and complaint',
                           data=build_package(record.complaint_id, values),
                           file_name='bbb_complaint_helper.zip', mime='application/zip',
                           disabled=(not approved or bool(blocked)), key=prefix + 'download')
        st.caption('Extract the ZIP and follow README.txt. Upload evidence and answer eligibility/declaration questions directly in BBB. Do not refile an uncertain submission.')
        reference = st.text_input('BBB reference number received after submission', key=prefix + 'reference')
        completed = st.checkbox('BBB confirmed my submission and I copied its reference number above.', key=prefix + 'completed')
        if st.button('Record BBB submission', disabled=not(completed and reference.strip()), key=prefix + 'record'):
            try:
                record_submission(db_path, record.complaint_id, record.user_email, reference)
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(str(exc))
