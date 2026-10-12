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
    'complaint.reference','complaint.date','jurisdiction.country','jurisdiction.state',
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


def from_native(raw, owner_email):
    """Export explicit stored facts. Do not mine unrelated names/numbers from logs."""
    aliases = {
        'consumer.full_name': ('user_name', 'consumer_name', 'plaintiff_name'),
        'consumer.first_name': ('user_first_name', 'first_name'),
        'consumer.last_name': ('user_last_name', 'last_name'),
        'consumer.phone': ('user_phone', 'consumer_phone', 'plaintiff_phone'),
        'consumer.street': ('user_street', 'consumer_street', 'plaintiff_street'),
        'consumer.city': ('user_city', 'consumer_city', 'plaintiff_city'),
        'consumer.state': ('user_state', 'consumer_state', 'plaintiff_state'),
        'consumer.postal_code': ('user_zip', 'consumer_postal_code', 'plaintiff_zip'),
        'consumer.country': ('user_country', 'consumer_country'),
        'business.name': ('company_name', 'defendant_legal_name'),
        'business.email': ('company_email',), 'business.phone': ('company_phone',),
        'business.street': ('company_street', 'defendant_street'),
        'business.city': ('company_city', 'defendant_city'),
        'business.state': ('company_state', 'defendant_state'),
        'business.postal_code': ('company_zip', 'defendant_zip'),
        'business.country': ('company_country',),
        'complaint.subject': ('subject',),
        'complaint.narrative': ('complaint_professional', 'complaint_raw', 'claim_detail'),
        'complaint.requested_resolution': ('requested_resolution', 'desired_resolution'),
        'complaint.amount': ('amount_text', 'amount', 'disputed_amount'),
        'complaint.transaction_date': ('transaction_date', 'purchase_date'),
        'complaint.reference': ('order_number', 'invoice_number', 'booking_reference'),
        'complaint.date': ('created_at',),
        'jurisdiction.country': ('filing_country',), 'jurisdiction.state': ('filing_state',),
    }
    values = {}
    for key, sources in aliases.items():
        group, field = key.split('.')
        nested = raw.get(group)
        candidates = ([nested.get(field)] if isinstance(nested, dict) else []) + [raw.get(k) for k in sources]
        value = next((v for v in candidates if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip()), '')
        values[key] = str(value)
    values['consumer.email'] = owner_email
    # A structured goal is usable; do not generate a remedy the user never requested.
    strategy = raw.get('strategy')
    if not values.get('complaint.requested_resolution') and isinstance(strategy, dict):
        goal = strategy.get('primary_goal')
        if isinstance(goal, str): values['complaint.requested_resolution'] = goal
    if values.get('complaint.date'):
        from datetime import datetime, timezone
        try:
            date = values['complaint.date']
            parsed = datetime.fromtimestamp(float(date), timezone.utc) if date.replace('.', '', 1).isdigit() else datetime.fromisoformat(date.replace('Z', '+00:00'))
            values['complaint.date'] = parsed.date().isoformat()
        except (ValueError, OverflowError, OSError):
            values['complaint.date'] = ''
    return values


def from_escalation(record):
    values = from_native(record.raw_json or {}, record.user_email or '')
    values.update({'consumer.full_name': record.user_name or '', 'business.name': record.company_name or '',
                   'complaint.subject': record.subject or '', 'complaint.narrative': record.display_text()})
    return values


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


def render_export_panel(complaint_id, initial_values, verify_owner=None, db_path=None):
    import streamlit as st
    import os
    import sys
    from pathlib import Path
    if verify_owner:
        verify_owner()
    # Optional bridge setup cannot block the existing application.
    sync_error = None
    if db_path:
        try:
            phone_dir = Path(os.environ.get('CW_PHONE_CALL_DIR', str(Path(__file__).resolve().parents[1] / 'phone_call')))
            if str(phone_dir) not in sys.path: sys.path.insert(0, str(phone_dir))
            from cw_auth import COOKIE_NAME, TOKEN_DB
            from cw_extension_bridge import activate
            activate(st.context.cookies.get(COOKIE_NAME), complaint_id, db_path, TOKEN_DB)
        except (ImportError, AttributeError, PermissionError, LookupError, ValueError, OSError) as exc:
            sync_error = 'Sign in through CW and check the Chrome bridge deployment files.'
        except Exception:
            sync_error = 'Chrome sync is unavailable. Check the server database configuration.'
    with st.expander('Chrome Form Assistant: automatic filling'):
        st.write('The selected database complaint is used automatically by the connected extension. Open the third-party form, click the extension, then review and edit the website itself.')
        if db_path and not sync_error:
            st.success('Selected complaint is ready for automatic Chrome sync.')
        elif sync_error:
            st.info(sync_error)
        st.caption('Configure your CW HTTPS address once in extension Settings. Missing facts are left blank; files, declarations and submission stay manual.')
        try:
            data = build_export(complaint_id, initial_values)
            prefix = hashlib.sha256(data).hexdigest()[:20]
            if verify_owner: verify_owner()
            st.download_button('Download JSON backup (optional)', data=data, file_name='cw_complaint.json',
                               mime='application/json', key='cw_ext_backup_' + prefix)
        except ValueError as exc:
            st.info('JSON backup unavailable: ' + str(exc))
