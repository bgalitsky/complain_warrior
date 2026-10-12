"""Authenticated Streamlit handoff to the local California BreEZe helper."""
import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from california_companion import DIRECTORY, ENTRY, case_key


def build_package(complaint_id, values):
    payload = {'version': 1, 'state': 'CA', 'portal': 'breeze',
               'complaint_id': str(complaint_id), 'values': values}
    case_key(payload)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('complaint.json', json.dumps(payload, ensure_ascii=False, indent=2))
        for name in ('california_companion.py', 'bbb_companion.py', 'README_CALIFORNIA.md'):
            archive.write(Path(__file__).with_name(name), name)
        archive.writestr('requirements.txt', 'playwright>=1.45,<2\n')
    return output.getvalue()


def receipt(db_path, complaint_id, email, reference=None):
    email = email.strip().lower()
    if not email:
        raise ValueError('A verified owner is required.')
    if reference is not None and (not reference.strip() or len(reference) > 200):
        raise ValueError('Enter a reference of 1–200 characters.')
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS cw_ca_submissions ('
                   'complaint_id TEXT NOT NULL, user_email TEXT NOT NULL, '
                   'reference TEXT NOT NULL, recorded_at TEXT NOT NULL, verification TEXT NOT NULL, '
                   'PRIMARY KEY (complaint_id,user_email))')
        if reference is not None:
            db.execute('INSERT INTO cw_ca_submissions VALUES (?, ?, ?, ?, ?) '
                       'ON CONFLICT(complaint_id,user_email) DO NOTHING',
                       (str(complaint_id), email, reference.strip(),
                        datetime.now(timezone.utc).isoformat(), 'user_reported'))
        return db.execute('SELECT reference, recorded_at FROM cw_ca_submissions '
                          'WHERE complaint_id=? AND user_email=?', (str(complaint_id), email)).fetchone()


def render_california_panel(record, db_path):
    import streamlit as st
    from shared_login import require_owner
    require_owner(record.user_email)
    prefix = 'ca_' + hashlib.sha256((str(db_path) + str(record.complaint_id) + record.user_email).encode()).hexdigest()[:20]
    with st.expander('California: assisted BreEZe filing with approved automatic submission'):
        st.write('California routes complaints by profession. Check the directory, then use this helper only if the responsible board accepts your complaint through BreEZe.')
        st.link_button('Find the correct California board', DIRECTORY)
        st.link_button('Open BreEZe', ENTRY)
        st.warning('Pilot integration: live portal behavior has not been verified. Other board portals and the DCA agency-feedback form are not automated.')
        saved = receipt(db_path, record.complaint_id, record.user_email)
        if saved:
            st.success(f'User-reported California submission: {saved[0]} ({saved[1]}).')
            return
        values = {}
        for key, label, default in (
            ('full_name', 'Consumer full name', record.user_name),
            ('business_name', 'Business / professional name', record.company_name),
            ('board', 'Responsible board (confirm in directory)', ''),
            ('license_number', 'License number, if known', ''),
            ('postal_address', 'Consumer postal address', ''),
            ('business_address', 'Business postal address', ''),
            ('phone', 'Consumer telephone', ''),
            ('transaction_date', 'Transaction date', ''),
            ('amount', 'Disputed amount', ''),
        ):
            values[key] = st.text_input(label, value=default or '', key=prefix + key)
        values['email'] = record.user_email
        st.caption('Consumer email: ' + record.user_email)
        values['narrative'] = st.text_area('California complaint narrative', value=record.display_text(), height=200, key=prefix + 'narrative')
        values['requested_resolution'] = st.text_area('Requested resolution', key=prefix + 'resolution')
        eligible = st.checkbox('I checked that the selected board uses BreEZe and accepts this type of complaint.', key=prefix + 'eligible')
        reviewed = st.checkbox('I reviewed these facts and have not already filed this complaint with this board.', key=prefix + 'reviewed')
        st.download_button('Download California filing helper',
                           data=build_package(record.complaint_id, values),
                           file_name='california_complaint_helper.zip', mime='application/zip',
                           disabled=not (eligible and reviewed and values['board'].strip() and values['narrative'].strip()),
                           key=prefix + 'download')
        st.caption('Run the helper locally. Log in and complete required manual steps in its browser. The helper fills approved fields and clicks Submit once only after your final approval. It cannot reuse a government login from an ordinary browser tab.')
        reference = st.text_input('California agency confirmation/reference number', key=prefix + 'reference')
        confirmed = st.checkbox('The agency confirmed my submission; this is its reference number.', key=prefix + 'confirmed')
        if st.button('Record California submission', disabled=not (confirmed and reference.strip()), key=prefix + 'record'):
            require_owner(record.user_email)
            receipt(db_path, record.complaint_id, record.user_email, reference)
            st.rerun()
