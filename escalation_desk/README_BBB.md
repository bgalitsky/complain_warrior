# BBB assisted filing

This adds a BBB panel immediately below the selected complaint, before letter generation.
It works when Streamlit is hosted on EC2 because Playwright runs on the consumer's computer.
No remote desktop, cookie transfer, browser debugging port, or server Playwright installation is required.

## Install in the existing application

Copy `cw_regulatory_complaint_sender.py`, `bbb_workflow.py`, and `bbb_companion.py`
into the existing `escalation_desk` directory. Keep the existing config and database.
Install the existing `requirements_cw_sender.txt` and restart Streamlit as usual.
The module uses Streamlit's existing link/download controls; Playwright is only a local companion dependency.

## Consumer workflow

1. Select the consumer and complaint in the escalation desk.
2. Open the BBB panel and review/edit the narrative, contact details, business and desired resolution.
3. Confirm the information and download the ZIP. Extract it on the consumer's computer.
4. Follow its README: install Playwright and Chromium, then run `python bbb_companion.py`.
5. Start the BBB complaint, complete login/MFA/CAPTCHA manually and select the correct business.
6. At the terminal prompt choose the BBB tab, confirm the business, select a visible form field,
   and map a supplied complaint value (or type a missing value). Confirm the preview.
7. Repeat as necessary; the helper rescans after each operation. Navigate to subsequent steps manually.
8. Upload selected evidence manually, review all declarations and click Submit in the browser.
9. Record the BBB reference in the app after BBB confirms. If uncertain, inspect the existing case instead of resubmitting.

No guessed BBB field labels or automatic submit/next buttons are included. The live form is
inspected at runtime, so the user can map its current labels without editing source code.
Text/email/tel/number/date/url fields, textareas and single-select controls are supported.
Unlabelled controls, custom widgets, third-party frames, declarations and file uploads are manual.
The companion never executes webpage instructions or sends content to an LLM.

## Storage and deployment boundaries

`cw_bbb_submissions` is created on the first user-reported completion. Its primary key is
(complaint_id, user_email), providing duplicate-record protection and a BBB-specific history.
Existing main complaint statuses are not automatically changed: BBB is not a government authority,
and the receipt is explicitly user-reported, not independently verified by the worker.
A previously recorded BBB case suppresses another helper download for that complaint.

Packages contain personal data and should be stored privately/deleted after use. Evidence is
not bundled. A temporary browser profile holds login state for the current run only and is
removed on normal close. Session expiration and browser crashes require manual recovery.
Do not assume values are private until Submit: forms may autosave.

The existing app's typed-email filter is not authentication. This change does not upgrade it.
Keep the escalation desk behind your authenticated application/access control; for multi-user
public deployment bind complaint access to verified identity, not a user-entered email address.

## Validation

Run from the repository root:

```
python -m pip install playwright
python -m playwright install chromium
python -m unittest discover -s escalation_desk/tests -v
```

Tests use intercepted synthetic BBB-origin pages; they do not submit a complaint or contact
BBB. Live BBB login, regional form variants and final confirmation still require a manual
smoke test with the intended consumer. No bypass of anti-bot controls is implemented.

New helper downloads are disabled for native complaints already marked resolved, filed in
small claims court, or escalated. Existing completed submissions may still be recorded.

### Validation in this delivery

Python compilation and four non-browser tests passed. The three browser tests are included,
but could not execute here because the Chromium download returned an invalid/truncated
archive and no installed browser was available. Run the full test command above after
installing Chromium. The live BBB workflow has not been exercised or submitted.
