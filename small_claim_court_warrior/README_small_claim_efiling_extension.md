# Small Claim Court Warrior — California e-filing extension

## Files

- `small_claim_court_warrior_efiling.py` — complete replacement for the current Streamlit module.
- `requirements_small_claim_court_warrior_efiling.txt` — dependencies; no new third-party package is required.
- `small_claim_court_warrior_efiling.patch` — unified diff against `small_claim_court_warrior(10).py`.

## Run

```bash
pip install -r requirements_small_claim_court_warrior_efiling.txt
streamlit run small_claim_court_warrior_efiling.py
```

Place the module beside the production `cw_store.sqlite`, or use the existing database path/upload controls.

## What was added

1. Confirmed-county allow-list and provider/court routing.
2. PDF checks for encryption, file size, page count, searchable text, and active form fields.
3. Flattening of the populated SC-100 PDF.
4. EFSP-ready ZIP containing separate PDFs, a manifest, hashes, and upload instructions.
5. `small_claim_efilings` SQLite table for handoff, submission, acceptance, and rejection tracking.
6. EFSP receipt and court case-number recording.
7. Optional API submission through a private/contracted EFSP gateway.
8. Correction of the old behavior that marked a case submitted when only a draft packet had been saved.

## Optional EFSP API gateway

Most consumer EFSP portals do not expose a public filing API. The module therefore defaults to assisted handoff. If you obtain an enterprise EFSP integration or operate your own gateway, configure:

```bash
export CW_EFSP_API_URL='https://your-gateway.example/v1/california/small-claims/filings'
export CW_EFSP_API_TOKEN='replace-with-secret-token'
export CW_EFSP_PROVIDER_NAME='Your EFSP Gateway'
```

The module sends a multipart `POST`:

- `metadata`: JSON string containing county, filing stage, parties, amount, and document manifest.
- `documents`: one repeated multipart item per PDF.
- `Authorization: Bearer <token>` when a token is configured.

A successful JSON response may return any of:

```json
{
  "envelope_reference": "...",
  "envelope_id": "...",
  "transaction_id": "..."
}
```

## Workflow

1. Generate the packet.
2. Populate the official SC-100 PDF.
3. Open **California Small Claims e-file submission**.
4. Validate the lead SC-100 and add any separate official forms.
5. Download the EFSP-ready ZIP.
6. Open an approved EFSP, upload the documents, and submit.
7. Enter the EFSP envelope number and upload the receipt.
8. Only then does Complaint Warrior mark the case as submitted to small claims court.

## Database migration

The module creates these tables automatically when needed:

- `small_claim_packets`
- `small_claim_efilings`

It does not require a manual migration and does not modify `cw_companies.sqlite`.
