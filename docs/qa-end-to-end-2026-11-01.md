# GovBills end-to-end QA — 1 November 2026

## Scope and safety

Testing used the development preview, not the published application. All new
financial records are synthetic. No bank transfer, email, SMS or provider advice
was sent. Existing invoices and provider statements were not changed.

Provider statement imports supersede evidence within their scope, so the
financial lifecycle was completed in a new, isolated site under MOHW:
`TEST-E2E-NOV01`, with CEB account `TEST-E2E-NOV01-CEB`.

## Browser journey completed

| Stage | Outcome |
| --- | --- |
| Authentication | Clerk and finance sign-in and verification worked; role handoffs used separate demo actors. |
| Intake | Two synthetic PDFs were extracted, checked once and imported once. Individual-file, ZIP and folder controls were present. |
| Capture-once preparation | Existing imported records were reused in batch preparation without another upload. |
| Submission guard | Batch `BATCH-2026-000011` remains in Preparation: missing provider evidence correctly disables submission. |
| Provider reconciliation | An owned MUR 126 mismatch was detected; corrected isolated statement #12 matched MUR 125 and MUR 200. |
| Independent approval | Clerk submitted batch `BATCH-2026-000012`; verifier and budget holder completed their distinct stages. |
| Settlement | `SI-2026-000008`, MUR 325, was prepared/signed by finance and counter-signed by a separate payment authoriser. |
| Synthetic bank outcome | In-app test submission/execution used reference `TEST-SIMULATED-NO-TRANSFER-NOV01`. No external payment occurred. |
| Bank matching | Statement #4, row #8, matched one of one test transfers to the MUR 325 instruction, with no investigation rows. |
| Invoice copies | Invoices #41 and #42 have downloadable operational stamped PDFs showing approval, batch, settlement and explicit no-transfer reference. |
| Provider advice | The retained invoice-entry CSV contained only the two test invoices and totalled MUR 325. Delivery was left unrecorded and unsent. |
| Audit | Batch, settlement, signing, file preparation, synthetic execution and bank-match evidence were inspectable. |
| Visibility | Account filters, My space, overdue totals, overview drilldown and bank insights loaded. A MOHW clerk could not view a finance-visible MOE account. |
| Training | Nine role manuals, chapter navigation and transcripts were visible. Narration playback had separate prior verification; it was not repeated in this financial pass. |

The isolated batch is Settled and its two invoices are Paid **only as synthetic
development fixtures**. The original blocked batch remains unchanged for
readiness inspection.

## Fixes made

- Prevented early-month demo generation from producing future issue or recording
  timestamps. Normal business-date validation remains enforced.
- Corrected bank search to recognise batch references, including membership in a
  grouped settlement. The batch reconciliation shortcut now uses that search.
- Reduced OCR preview crowding by showing only the selected field's annotation.
  Template-editor annotations retain their existing behaviour.
- Replaced CSP-blocked inline style attributes with external stylesheet classes;
  the strict security policy was not weakened.
- Kept video-authoring helpers compatible with both the standalone Django
  repository and Replit's workspace layout.

## Automated and build verification

- Initial full suite: **514 passed, 1 failed**. The failure exposed the
  early-month demo-date bug above.
- Corrected acceptance and narration/layout-portability rechecks: **4 passed**,
  including the complete seeded signed-payment acceptance test.
- OCR/CSP presentation regressions: **2 passed**.
- Bank reconciliation regressions: **22 passed**, including new direct-batch and
  grouped-membership search cases.
- Django system checks: no issues.
- Migration drift check: no changes detected.
- Static collection and manifest post-processing: successful.
- Source whitespace validation: clean.

These are a full initial suite plus focused rechecks, not a claim that every
test was rerun after every edit.

## Not certified by this run

This is not a production security, performance or live-integration sign-off.
The corrected batch-to-bank shortcut passed its post-restart browser check.
Final browser confirmation of the OCR annotation change remained incomplete;
its template/stylesheet regression checks passed. Intermittent browser
inline-style CSP warnings were not conclusively traced to application code.
The known authored inline styles were removed without relaxing CSP; the remaining
browser diagnostic is not represented as resolved.

The final bank-specific file layout is still pending. Live bank execution,
external provider advice delivery, Outlook connectivity and SMS/email delivery
were deliberately not exercised. Requested execution dates and actual bank
value dates remain separate evidence; the synthetic case used a 1 November
value date despite a later requested execution date.
