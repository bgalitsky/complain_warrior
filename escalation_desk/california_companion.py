"""Local California BreEZe assistant: reviewed fields and one approved submit click."""
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bbb_companion import scan_fields, fill_field, load_payload

DIRECTORY = 'https://www.dca.ca.gov/consumers/complaints/consumer.html'
ENTRY = 'https://www.breeze.ca.gov/datamart/complaintRsdModuleMain.do'


def allowed_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname == 'www.breeze.ca.gov'
                and p.port in (None, 443) and not p.username and not p.password)
    except ValueError:
        return False


def case_key(data):
    email = data['values'].get('email', '').strip().lower()
    case = str(data.get('complaint_id', '')).strip()
    if data.get('state') != 'CA' or data.get('portal') != 'breeze' or not email or not case:
        raise ValueError('Expected a California BreEZe package with an owner and case ID.')
    return hashlib.sha256(json.dumps([email, case, 'CA'], separators=(',', ':')).encode()).hexdigest()


class Attempts:
    """Durable local at-most-once click guard; an unknown outcome never auto-retries."""
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (case_key TEXT PRIMARY KEY, '
                       'status TEXT NOT NULL, updated_at TEXT NOT NULL, reference TEXT NOT NULL)')

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT status, reference FROM attempts WHERE case_key=?', (key,)).fetchone()

    def reserve(self, key):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?)',
                       (key, 'outcome_unknown', datetime.now(timezone.utc).isoformat(), ''))

    def confirm(self, key, reference):
        if not reference.strip() or len(reference) > 200:
            raise ValueError('A reference of 1–200 characters is required.')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE attempts SET status=?, reference=?, updated_at=? WHERE case_key=?',
                       ('user_reported', reference.strip(), datetime.now(timezone.utc).isoformat(), key))


def fingerprint(page):
    if not allowed_url(page.url):
        raise ValueError('Return to the official BreEZe page.')
    # Include review text, declarations, hidden server fields and all current values,
    # except password values. Store only the digest, never form data, in the ledger.
    state = page.evaluate('''() => ({url: location.href, text: document.body.innerText,
        controls: Array.from(document.querySelectorAll('input,textarea,select')).map(e =>
            [e.id,e.name,e.type,e.type === 'password' ? '' : e.value,e.checked])})''')
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def submit_once(page, button, approved_fingerprint, ledger, key):
    if fingerprint(page) != approved_fingerprint:
        raise ValueError('Page changed after review; inspect and approve it again.')
    if not button.is_visible() or not button.is_enabled():
        raise ValueError('Selected submit button is unavailable.')
    if not page.evaluate("() => Array.from(document.forms).every(f => f.checkValidity())"):
        raise ValueError('Complete the required fields in the browser first.')
    # Commit BEFORE the network-capable click; timeouts/crashes leave outcome unknown.
    ledger.reserve(key)
    button.click(timeout=15000, no_wait_after=True)


def fill_one(page, data):
    fields = scan_fields(page, allowed_url)
    for i, (_, _, info) in enumerate(fields, 1):
        print(i, info['label'])
    n = int(input('Field number (0 to cancel): '))
    if n == 0:
        return
    if not 1 <= n <= len(fields):
        raise ValueError('Invalid field number')
    item = fields[n - 1]
    if item[2]['tag'] == 'select':
        choices = [(o['label'], o['value']) for o in item[2]['options'] if not o['disabled']]
    else:
        choices = [(k, v) for k, v in data['values'].items() if v.strip()]
    for i, (label, _) in enumerate(choices, 1):
        print(i, label)
    n = int(input('Value number (0 to type a value): '))
    if n == 0:
        value = input('Value: ')
    elif 1 <= n <= len(choices):
        value = choices[n - 1][1]
    else:
        raise ValueError('Invalid value number')
    print('Target:', item[2]['label'], '\nValue:', value)
    if input('Type FILL to approve: ').strip() == 'FILL':
        warning = fill_field(page, item, value, allowed_url)
        print('Value verified.', warning)


def approve_submit(page, data, ledger, key):
    if ledger.get(key):
        raise ValueError('A previous attempt exists. Check with the agency; do not retry.')
    # Bind a concrete DOM element, not a locator that can silently retarget.
    buttons = []
    for element in page.query_selector_all('button,input[type=submit],input[type=button]'):
        label = element.evaluate("e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()")
        if element.is_visible() and element.is_enabled() and label.lower() in (
                'submit', 'submit complaint', 'file complaint'):
            buttons.append((element, label))
    if not buttons:
        raise ValueError('No supported final submit button on this page. Complete manually.')
    for i, (_, label) in enumerate(buttons, 1):
        print(i, label)
    n = int(input('Final complaint submit button number: '))
    if not 1 <= n <= len(buttons):
        raise ValueError('Invalid button')
    snapshot = fingerprint(page)
    print('Case:', data['complaint_id'], 'Business:', data['values'].get('business_name', ''))
    print('Review the entire complaint, recipient board, evidence and declarations in the browser.')
    print('Selected button:', buttons[n - 1][1], 'on', page.url)
    phrase = 'SUBMIT ' + str(data['complaint_id'])
    if input('Authorize one final complaint submission by typing ' + phrase + ': ').strip() != phrase:
        return
    submit_once(page, buttons[n - 1][0], snapshot, ledger, key)
    print('Submit clicked once. Outcome is UNKNOWN until you inspect the agency response.')


def run(path):
    from playwright.sync_api import sync_playwright
    data = load_payload(path)
    key = case_key(data)
    state_dir = Path.home() / '.complaint-warrior'
    state_dir.mkdir(mode=0o700, exist_ok=True)
    ledger = Attempts(state_dir / 'california-attempts.sqlite')
    print('California BreEZe. Use only for a board that accepts your complaint through BreEZe.')
    print('Routing directory:', DIRECTORY)
    print('Login, CAPTCHA, board/license selection, navigation, uploads and declarations are manual.')
    print('Existing local attempt:', ledger.get(key) or 'none')
    with tempfile.TemporaryDirectory(prefix='cw-california-') as profile:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(ENTRY, wait_until='domcontentloaded')
            while True:
                command = input('\nf=fill, s=approved submit, r=record confirmed reference, q=quit: ').strip().lower()
                if command == 'q':
                    break
                try:
                    if command == 'r':
                        if not ledger.get(key):
                            raise ValueError('No helper submission attempt recorded.')
                        reference = input('Reference displayed by the agency after confirmed submission: ')
                        ledger.confirm(key, reference)
                        print('Saved as USER-REPORTED. Copy this reference to Complaint Warrior.')
                        continue
                    if command not in ('f', 's'):
                        continue
                    if ledger.get(key):
                        raise ValueError('Existing attempt: verify its outcome before any further filing.')
                    tabs = [t for t in context.pages if not t.is_closed() and allowed_url(t.url)]
                    for i, t in enumerate(tabs, 1):
                        print(i, t.url)
                    n = int(input('BreEZe tab number: '))
                    if not 1 <= n <= len(tabs):
                        raise ValueError('Invalid tab')
                    page = tabs[n - 1]
                    if command == 'f':
                        fill_one(page, data)
                    else:
                        approve_submit(page, data, ledger, key)
                except Exception as exc:
                    print('Stopped:', type(exc).__name__, str(exc))
                    print('No automatic retry. Inspect the browser and any existing attempt.')
            context.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('payload', nargs='?', default=str(Path(__file__).with_name('complaint.json')))
    try:
        run(parser.parse_args().payload)
    except KeyboardInterrupt:
        print('Stopped. Check the agency before retrying an uncertain submission.')
