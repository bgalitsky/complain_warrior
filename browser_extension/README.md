# Complaint Warrior Chrome Form Assistant — pilot v0.1

An installable Manifest V3 extension that discovers supported fields on a new website,
proposes mappings from a CW complaint, and fills the fields you approve in your existing
browser tab. It does not need Playwright or a second browser. Your existing agency login
stays in that tab; passwords and cookies are not read or exported.

## Install in Chrome (Windows, macOS or Linux)

1. Extract this ZIP.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select **browser_extension/chrome**, the folder containing
   `manifest.json`. Do not select the whole ZIP or repository root.
4. Pin **Complaint Warrior Form Assistant** using Chrome's Extensions menu.

No npm/Python installation is required to run the extension itself. This is a developer
build, not a published Chrome Web Store listing. Only `activeTab`, `scripting` and
`storage` permissions are requested. There is no blanket access to all websites, no
background site monitoring, no external AI service, and no analytics.

## Connect the CW export

Overlay the supplied folders onto your Complaint Warrior repository. Keep your existing
server database/configuration; do not overwrite those with sample data. The archive
includes the earlier module updates needed by these versions of escalation desk and
Small Claim Court Warrior. Restart the relevant Streamlit applications.

- **Escalation desk:** Select a complaint. Expand **Chrome Form Assistant: export this
  complaint for a new website**. This exporter rechecks the existing verified owner.
- **Small Claim Court Warrior:** Generate the court packet. The same exporter appears
  in Downloads, using the reviewed SC-100 data. The upstream small-claims module still
  uses its existing typed-email filter; this extension update does not convert it to
  secure multi-user authentication. Restrict that deployment until server ownership
  and shared login have been implemented throughout it.

Review the export fields, including consumer/business addresses, transaction reference,
requested resolution, and intended country/state. Check the review box and click
**Export for Chrome Form Assistant**. A local `cw_complaint.json` file is downloaded.
This explicit file handoff is the implemented CW connection; there is no hidden API
connection or automatic database scraping.

## Fill a new site

1. Find the official agency and check that it handles your complaint and jurisdiction.
   This extension does not select the legal recipient or search for an agency.
2. Open its actual complaint form and log in normally if required.
3. Click the extension and import `cw_complaint.json`.
4. Click **Discover fields in this tab**.
5. Review the displayed destination and each proposed value. Choose a different CW
   source or enter a value if the suggestion is wrong or unavailable. Dropdown values
   must be one of the site's actual options. Select the fields to fill, or use
   **Select strong suggestions**, which selects only unfilled, strongly matched fields.
6. Approve any replacement of an existing value individually. Check the final review
   box and click **Fill selected fields**. Read the per-field result and inspect the site.
7. Complete navigation, CAPTCHA, uploads, declarations, signatures and final submission
   yourself. Discover again on each next page or after a dynamic form update.
8. Click **Forget complaint data** when finished and delete the exported file if no
   longer needed. Forget does not erase values already sent to the website.

The complaint is retained in Chrome's extension session storage, not sync/local storage.
Closing the popup does not lose it; restarting the browser clears session storage.
A site can autosave when fields change, even before final submission.

## What discovery actually does

The extension reads visible, editable standard text/date/number/email/phone inputs,
textareas and single-select dropdowns. It considers labels, ARIA labels, placeholders,
autocomplete hints, and fieldset/group labels to distinguish consumer/business data.
English rules plus selected Spanish/French label synonyms are included. It proposes
matches for unfamiliar sites with those semantics; it is not a universal multilingual
AI agent. Unrecognised fields/languages need manual mappings. Scores are heuristic
rule scores, not calibrated probabilities. No missing facts are invented or translated.

Open shadow roots and accessible same-origin iframe documents can be scanned. Closed
shadow roots, cross-origin frames, custom dropdown widgets, PDFs, and unlabelled fields
are manual. HTML address fields do not establish the proper legal venue or recipient.
Country/state data is kept separate for consumer residence, business location, and
filing jurisdiction. The user must validate which one a form asks for.

Passwords, hidden inputs, files, radio buttons, checkboxes and recognizable sensitive
credential/payment/signature fields are excluded. No button is clicked. It never clicks
Next or Submit. Filling dispatches ordinary input/change events; it does not promise
compatibility with every site's custom JavaScript.

Before filling, it checks that the URL, original DOM elements, labels/options, and
current values still match the reviewed snapshot. If a field changes, it refuses or
stops the partial batch. A DOM replacement or rejected value is reported as unverified.
There is no automatic retry or rollback because websites may already have autosaved.

## Local demo

From the repository root run:

    python -m http.server 8765 --directory browser_extension/tests

Open `http://localhost:8765/demo.html`, click the extension and import
`browser_extension/tests/example_complaint.json`. All data is synthetic. Discover and
fill; name, email, state, business details and narrative should be proposed. Password,
CAPTCHA, file and declaration controls must remain untouched. The page's final submit
message should appear only if you click the page's submit button yourself.

The extension otherwise requires HTTPS; HTTP is allowed only for localhost/127.0.0.1.

## Validation and deployment status

17 automated tests passed: six matching/schema tests, seven DOM-behavior tests and four
Python exporter tests. JavaScript syntax and Python compilation checks passed. DOM tests
use jsdom and simulated layout, not Chrome. Chrome's browser download failed in the
build environment, so an actual installed-extension run and real agency workflows have
not been verified. Run the local demo in your Chrome before trying a real complaint.

To run development tests (Node 24 recommended):

    cd browser_extension
    npm install
    npm test
    python -m unittest discover -s tests -p 'test_*.py' -v

For commercial distribution, validate representative target sites with consented test
accounts, complete the CW server-side ownership/authentication work, and package/review
for Chrome Web Store distribution. This archive is a pilot, not a claim that arbitrary
agency forms can be completed reliably without supervision.
