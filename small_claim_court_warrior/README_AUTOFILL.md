# Small Claim Court Warrior: automatic SC-100 population

Overlay the `small_claim_court_warrior` folder onto the repository. Install:

    python -m pip install -r small_claim_court_warrior/requirements_small_claim_court_warrior.txt
    streamlit run small_claim_court_warrior/small_claim_court_warrior.py

Select your complaint, review the prefilled Intake data, and click **Generate packet**.
This now fetches the official SC-100 and automatically populates its factual fields.
Download the editable SC-100 from Downloads, complete the remaining items, review,
sign as required, and upload the completed lead document in the existing filing pane.
The separate flattened generated copy contains only the automatically populated facts;
it is not a completed or signed filing. Generating either copy does not submit it.

Supported template: SC-100 revision January 1, 2026, six pages. Source:
https://courts.ca.gov/documents/sc100.pdf
Form information: https://selfhelp.courts.ca.gov/jcc-form/SC-100
The downloaded official blank is bundled in `templates/sc100_2026.pdf` for regression
tests and as an upload fallback if fetching fails. A newer or different layout stops
automatic filling and requires updating the mapping; no field-name guesses are used.

Populated fields: first plaintiff's name, street/city/state/ZIP, phone, email; defendant
legal name and street/city/state/ZIP; agent name when supplied; amount; claim narrative;
damages calculation; plaintiff headers. The mapping also supports `incident_date` in
programmatic payloads. Intake currently has a final-demand date, so incident date is
left blank in the UI-generated PDF rather than confused with the demand date.

Court location, clerk-assigned case/hearing data, additional parties, eligibility and
venue declarations, signature and signing date must be completed/reviewed manually.
The existing county selection does not supply a courthouse address. Long text is
rejected without truncation; shorten it or prepare the appropriate attachment and
complete the form manually. Text unsupported by the source font is also rejected.

Implementation replaces the old fuzzy field matcher and silent exception swallowing.
It verifies canonical field values, widget values and appearance streams, retains an
editable copy, and verifies the flattened copy contains no form fields. Generated
artifacts are cleared when switching cases and when regenerating a packet. It also
normalizes the official PDF's reader-extension encryption before filling.

Run tests:

    python -m unittest discover -s small_claim_court_warrior/tests -v

Six tests exercise the actual official PDF, round-trip values, unmodified declarations,
flattening, unknown layouts, overflow, populated-template refusal and case changes.
A synthetic populated sample was rendered and visually checked. Full Streamlit and
live court/provider filing were not tested. No filing was submitted.

This update is form population, not browser/EFSP auto-submission. Existing provider
handoff and receipt logic remain. The upstream module's typed-email filter is not
secure authentication; this patch does not port the earlier shared-login system or
certify the upstream app for multi-user commercial deployment.
