"""Portable complaint data for the local Chrome Form Assistant; no auth tokens."""
import hashlib
import json

SCHEMA = 'cw.form-assistant.v1'
KEYS = (
    'consumer.full_name','consumer.first_name','consumer.last_name','consumer.email','consumer.phone',
    'consumer.street','consumer.city','consumer.state','consumer.postal_code','consumer.country',
    'business.name','business.email','business.phone','business.street','business.city','business.state',
    'business.postal_code','business.country','complaint.subject','complaint.narrative',
    'complaint.requested_resolution','complaint.amount','complaint.transaction_date',
    'complaint.reference','jurisdiction.country','jurisdiction.state',
)


def build_export(complaint_id, values):
    if not str(complaint_id).strip() or len(str(complaint_id)) > 200:
        raise ValueError('A complaint ID of 1–200 characters is required.')
    if set(values) - set(KEYS):
        raise ValueError('Unknown export fields.')
    if any(not isinstance(v, str) or len(v) > 30000 for v in values.values()):
        raise ValueError('Export values must be text of at most 30,000 characters each.')
    return json.dumps({'schema': SCHEMA, 'complaint_id': str(complaint_id), 'values': values},
                      ensure_ascii=False, indent=2).encode('utf-8')


def from_escalation(record):
    return {'consumer.full_name': record.user_name or '', 'consumer.email': record.user_email or '',
            'business.name': record.company_name or '', 'complaint.subject': record.subject or '',
            'complaint.narrative': record.display_text()}


def from_sc100(data):
    mapping = {'consumer.full_name':'plaintiff_name','consumer.email':'plaintiff_email',
               'consumer.phone':'plaintiff_phone','consumer.street':'plaintiff_street',
               'consumer.city':'plaintiff_city','consumer.state':'plaintiff_state',
               'consumer.postal_code':'plaintiff_zip','business.name':'defendant_legal_name',
               'business.street':'defendant_street','business.city':'defendant_city',
               'business.state':'defendant_state','business.postal_code':'defendant_zip',
               'complaint.narrative':'claim_detail','complaint.amount':'amount_text'}
    # The final demand date is NOT a transaction date. Court case_id is NOT an order number.
    return {key: str(data.get(source) or '') for key, source in mapping.items()}


def render_export_panel(complaint_id, initial_values, verify_owner=None):
    import streamlit as st
    if verify_owner:
        verify_owner()
    # New source data yields new widgets rather than stale exports after regeneration.
    prefix = 'cw_ext_' + hashlib.sha256(build_export(complaint_id, initial_values)).hexdigest()[:20]
    with st.expander('Chrome Form Assistant: export this complaint for a new website'):
        st.write('Import this file in the Chrome extension on the agency’s form. The extension uses your existing browser session and proposes field mappings for review.')
        st.caption('Confirm the official agency and jurisdiction yourself. Consumer residence, business location and filing jurisdiction are separate fields. Do not put passwords, card numbers or identity documents in this export.')
        values = {}
        for key in KEYS:
            default = initial_values.get(key, '')
            label = key.replace('.', ' / ').replace('_', ' ')
            if key in ('complaint.narrative', 'complaint.requested_resolution'):
                values[key] = st.text_area(label, value=default, key=prefix + key)
            else:
                values[key] = st.text_input(label, value=default, key=prefix + key)
        approved = st.checkbox('I reviewed the data to export to the browser extension.', key=prefix + 'review')
        try:
            data = build_export(complaint_id, values)
            if verify_owner:
                verify_owner()
            st.download_button('Export for Chrome Form Assistant', data=data,
                               file_name='cw_complaint.json', mime='application/json',
                               disabled=not approved, key=prefix + 'download')
        except ValueError as exc:
            st.error(str(exc))
        st.caption('The JSON contains personal complaint data. Keep it private and remove it when finished. Exporting does not file or submit a complaint.')
