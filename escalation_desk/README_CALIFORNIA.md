# California complaint filing pilot

This adds a California panel to `cw_regulatory_complaint_sender.py` while preserving
shared Complaint Warrior authentication and the BBB helper. It is an assisted
BreEZe integration, not an unattended statewide filing service. The live portal
could not be inspected successfully in this environment; no real complaint was filed.

California uses different boards and portals. First consult:
https://www.dca.ca.gov/consumers/complaints/consumer.html
The directory links several professions to BreEZe:
https://www.breeze.ca.gov/datamart/complaintRsdModuleMain.do
Use this helper only after confirming the responsible board and complaint category.
Other portals require manual filing. Do not use DCA's Public Comments, Complaints
and Suggestions form for a complaint against a business: that form describes
feedback about DCA boards and programs.

## Run the downloaded helper

1. Extract the ZIP to a private folder. Install Python 3.10 or later.
2. Run `python -m pip install -r requirements.txt`.
3. Run `python -m playwright install chromium`.
4. Run `python california_companion.py` (on Windows, `py` may replace `python`).
5. In its Chromium window, choose the board/license and complete authentication,
   eligibility questions, CAPTCHA, navigation, declarations and evidence uploads.
6. In the terminal choose `f`, the tab, field and complaint value. Approve each
   field mapping. Unsupported controls and non-BreEZe frames stay manual.
7. On the final review page choose `s`. Choose the observed final submit button,
   review the entire complaint and recipient in the browser, then type the displayed
   `SUBMIT <case-id>` phrase. The helper clicks that button once. If its button label
   is unsupported, file manually; the helper does not guess a selector or endpoint.
8. Inspect the response. A click, navigation or timeout is NOT confirmation.
   Use `r` only after the agency provides a reference. Copy the reference into the
   California panel and explicitly record it as user-reported.
9. Quit with `q`. Delete the downloaded complaint package when no longer needed.

The temporary browser profile retains manual login for this run and is removed on
normal exit. No browser passwords or cookies are exported to the hosted app. The
hosted panel uses the existing verified app cookie and owner checks. A government
session is separate from Complaint Warrior authentication; this version does not
attach to the user's original Chrome profile. Closing the helper can lose progress.

## Reliability and commercial deployment limits

Before clicking, the helper checks the exact HTTPS BreEZe origin, page fingerprint,
button availability and HTML form validity. It stores an attempt transactionally in
`~/.complaint-warrior/california-attempts.sqlite` BEFORE clicking. A timeout or crash
leaves `outcome_unknown`; subsequent automatic attempts for the same owner/case in
that local ledger are blocked. Changing narrative or downloading again does not
change the case key. It never retries a submit automatically.

This is LOCAL duplicate protection: it cannot prevent another computer, a different
OS user or a manual submission from filing twice. Do not delete the ledger to retry.
If an attempt is uncertain, check with the agency. The web app stores only a
user-reported reference, scoped by owner and complaint. It does not claim browser-
verified success or set the overall government-escalation status automatically.
There is no hosted callback from the local helper.

Before offering automatic submission commercially, validate the actual target board
flow in an authorized test environment, establish a shared server-side attempt/receipt
protocol, and verify confirmation extraction. Existing application login isolation
does not make this pilot production-ready. Government sessions, declarations,
CAPTCHA, file uploads and nonstandard controls still require user interaction.

## Install into the repository

Overlay this update on the repository root. Keep the shared-login deployment settings
in `README_SHARED_LOGIN.md`; no new server-side Playwright dependency is needed.
The new files are `california_companion.py`, `california_workflow.py`, this document
and `tests/test_california.py`. The main app renders the California panel beside BBB.
The shared field scanner now accepts an explicit origin validator and excludes
embedded input/select text from field labels; its default remains BBB-only.

Run focused tests from the repository root:
`python -m unittest discover -s escalation_desk/tests -p test_california.py -v`

Tests use controlled fake browser objects and real temporary SQLite databases.
They verify origin boundaries, stale review refusal, duplicate attempts, uncertain
outcomes, package contents and owner-scoped receipts. They do not establish that the
live BreEZe portal accepts a complaint or has a supported final button.
