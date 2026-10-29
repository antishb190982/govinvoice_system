# Provider reconciliation

Finance officers and unit administrators upload a provider's pending-invoice listing at
**Payments → Reconciliation**. The statement covers one provider, organisational subtree,
currency and statement date.

## File contract

CSV (UTF-8), XLS or XLSX; maximum 10 MB and 10,000 invoice rows.
Use the first worksheet, with headers on its first row:

`account_number,invoice_number,amount,currency`

The first three columns are required. Currency is optional in the file and defaults to
the upload's currency. Account/invoice identifiers should be text, preserving leading
zeros. Amounts must be nonnegative full invoice amounts, not account balances, partial
payments or arrears. Decimal point is `.`, optionally with comma thousands separators.
No FX conversion, fuzzy matching, rounding tolerance, formulas or encrypted workbooks.

## Controls

- Matching requires the same provider, account, invoice reference, currency and amount,
  inside the statement's unit coverage. Reference comparison ignores case and outer whitespace.
- Duplicate references, missing invoices/accounts, amount/currency discrepancies, already
  paid/cancelled invoices and recorded invoices omitted from the listing go to Unsorted.
- Uploads do not approve invoices, alter original statement amounts or issue payments.
- Clerks can recheck their units' lines. Finance/unit administrators can reassign
  responsibility within their authorised scope, with a required recorded reason.
- Finance/unit administrators can return a current mismatched invoice for correction.
  Existing approvals/signatures remain in history; correction requires a new approval round.
  Active settlement instructions must first be cancelled using the existing payment controls.
- A newer statement replaces prior covered statements. Older dates cannot replace a newer
  current statement. Changed invoice details invalidate evidence and return its matched line
  to Unsorted. Both approval submission and approval decisions require current reconciliation.
- Finance selection, preparation, authorisation and bank release recheck evidence.
  Instruction item amounts and invoice fingerprints must still match preparation.
  A mixed valid/invalid selection fails atomically, rather than silently paying a subset.
- Evidence is pinned when an instruction is marked sent. Recording the actual bank execution
  can use that pinned evidence even if a later provider listing has cleared the debt.
  This does not permit changed invoice details or unverified bank release.

Statement originals remain in the portal's configured private file storage; access and
downloads are scoped. Audit records identify upload, matching, reassignment and correction
actors. Existing production invoices are **not** automatically grandfathered or reconciled.
Explicit demo generation supplies clearly named synthetic provider files for its own records.
