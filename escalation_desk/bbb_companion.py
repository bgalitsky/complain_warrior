"""Local, human-guided BBB form filler. No automatic navigation or submission."""
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def is_bbb_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (host == 'bbb.org' or host.endswith('.bbb.org'))


def load_payload(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('values'), dict):
        raise ValueError('Unsupported complaint package')
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in data['values'].items()):
        raise ValueError('All field values must be text')
    return data


def scan_fields(page):
    """Return visible editable text/select controls; never passwords or declarations."""
    fields = []
    if not is_bbb_url(page.url):
        raise ValueError('Return to the BBB complaint page before filling.')
    for frame in page.frames:
        if not is_bbb_url(frame.url):
            continue
        controls = frame.locator('input, textarea, select')
        for i in range(controls.count()):
            control = controls.nth(i)
            if not control.is_visible() or not control.is_enabled():
                continue
            info = control.evaluate('''el => ({
                tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
                label: Array.from(el.labels || []).map(label => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input, textarea, select, button')
        .forEach(node => node.remove());
    return (copy.textContent || '').trim();
}).join(' ').trim()
                    || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '',
                name: el.name || '', id: el.id || '', readOnly: !!el.readOnly,
                options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({
                    label: o.label, value: o.value, disabled: o.disabled
                })) : [], multiple: !!el.multiple
            })''')
            if info['readOnly'] or info['multiple']:
                continue
            if info['tag'] == 'input' and info['type'] not in ('text', 'email', 'tel', 'number', 'date', 'url'):
                continue
            # Unlabelled fields are left to manual interaction.
            if not info['label']:
                continue
            fields.append((frame, control, info))
    return fields


def fill_field(page, item, value):
    from playwright.sync_api import expect
    frame, control, info = item
    if not is_bbb_url(page.url) or not is_bbb_url(frame.url):
        raise ValueError('The browser left BBB. Rescan the complaint page.')
    # Reject stale/reordered controls after a dynamic page update.
    current = control.evaluate('''el => ({tag: el.tagName.toLowerCase(),
        type: (el.type || '').toLowerCase(), name: el.name || '', id: el.id || '',
        label: Array.from(el.labels || []).map(label => {
        const copy = label.cloneNode(true);
        copy.querySelectorAll('input, textarea, select, button')
        .forEach(node => node.remove());
    return (copy.textContent || '').trim();
}).join(' ').trim()
            || el.getAttribute('aria-label') || el.getAttribute('placeholder') || ''})''')
    if any(current[key] != info[key] for key in current):
        raise ValueError('The form changed. Rescan before filling.')
    if info['tag'] == 'select':
        if value not in [o['value'] for o in info['options'] if not o['disabled']]:
            raise ValueError('Select an available option.')
        control.select_option(value=value)
    else:
        control.fill(value)
    expect(control).to_have_value(value)
    return control.evaluate('el => el.validationMessage || ""')


def run(path):
    from playwright.sync_api import sync_playwright
    data = load_payload(path)
    print('Complaint:', data['complaint_id'])
    print('Business:', data['values'].get('business_name', ''))
    print('Login, CAPTCHA, business selection, navigation, uploads and Submit are manual.')
    print('BBB may save values as you enter them. Review the narrative before filling.')
    print('Type q at a page prompt to close. No credentials are sent to Complaint Warrior.')
    with tempfile.TemporaryDirectory(prefix='cw-bbb-') as profile:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://www.bbb.org/file-a-complaint', wait_until='domcontentloaded')
            while True:
                command = input('\nComplete manual steps in the browser, then Enter to scan (q to close): ').strip()
                if command.lower() == 'q':
                    break
                tabs = [tab for tab in context.pages if not tab.is_closed() and is_bbb_url(tab.url)]
                if not tabs:
                    print('No BBB tab. Finish authentication and return to BBB.'); continue
                for i, tab in enumerate(tabs, 1):
                    print(i, tab.url)
                try:
                    tab_number = int(input('BBB tab number: '))
                    if not 1 <= tab_number <= len(tabs):
                        raise ValueError('Invalid tab')
                    page = tabs[tab_number - 1]
                    if input('Confirm the selected business and current page are correct (yes): ').strip().lower() != 'yes':
                        continue
                    fields = scan_fields(page)
                    for i, (_, control, info) in enumerate(fields, 1):
                        print(f"{i}. {info['label']} [{info['type']}] (currently {'filled' if control.input_value() else 'empty'})")
                    if not fields:
                        print('No supported fields. Complete this step manually.'); continue
                    number = int(input('Field number to fill (0 to return to browser): '))
                    if number == 0:
                        continue
                    if not 1 <= number <= len(fields):
                        raise ValueError('Invalid field')
                    item = fields[number - 1]
                    if item[2]['tag'] == 'select':
                        options = [o for o in item[2]['options'] if not o['disabled']]
                        for i, option in enumerate(options, 1):
                            print(i, option['label'])
                        choice = int(input('Option number: '))
                        if not 1 <= choice <= len(options):
                            raise ValueError('Invalid option')
                        value = options[choice - 1]['value']
                        preview = options[choice - 1]['label']
                    else:
                        values = [(k, v) for k, v in data['values'].items() if v.strip()]
                        for i, (key, _) in enumerate(values, 1):
                            print(i, key)
                        choice = int(input('Complaint value number (0 to type a value): '))
                        if choice == 0:
                            value = input('Value: ')
                        elif 1 <= choice <= len(values):
                            value = values[choice - 1][1]
                        else:
                            raise ValueError('Invalid value')
                        preview = value
                    print('\nTarget:', item[2]['label'], '\nValue:', preview)
                    if input('Fill or replace this field? Type yes: ').strip().lower() == 'yes':
                        warning = fill_field(page, item, value)
                        print('Value verified.' + (' Form warning: ' + warning if warning else ''))
                except Exception as exc:
                    print('Not completed:', str(exc), '\nInspect the browser and rescan; no automatic retry.')
            context.close()
    print('If you submitted, record the BBB reference in Complaint Warrior. Do not resubmit an uncertain result.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('payload', nargs='?', default=str(Path(__file__).with_name('complaint.json')))
    args = parser.parse_args()
    try:
        run(args.payload)
    except KeyboardInterrupt:
        print('\nStopped. Check BBB before retrying any submission.')
