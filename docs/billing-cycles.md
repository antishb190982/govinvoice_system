# Provider billing cycles and automatic reconciliation

## Set up a cycle

Open **Billing cycle calendar** in the left navigation, then **New cycle**
(central master-data administrators only). Choose the provider, a unique code,
a meaningful name and the start/end dates.

One provider may have several distinct cycles with overlapping or identical
dates. A cycle is identified by its provider and code, not by its month.
The calendar places each cycle on its end date and displays its full period.
Use the month and provider filters to inspect a particular selection.

## Receive files without capturing details twice

**Upload Utility Records** and the batch wizard offer an optional common cycle.
For folders or spreadsheets spanning multiple cycles, leave this on Automatic:
each document/row is classified individually. A single source spreadsheet may
therefore be linked to several cycles.

Invoice capture uses an explicitly selected cycle, a unique configured period,
or an exact reference/account/amount/currency match in a processed provider file
covering that invoice's organisational scope. Overlapping/ambiguous cycles are
not guessed. Unknown records remain in **Awaiting cycle assignment**.

Unreadable documents or records whose provider is not yet identified remain
staged; provider/cycle classification cannot honestly be completed until those
details are known. Existing OCR confirmation and registration rules still apply.

## Reuse provider files already loaded

Select the named cycle when uploading a provider pending-invoice CSV/XLS/XLSX.
Uploading a newer file replaces evidence only within that cycle, currency and
organisational coverage. Files for other cycles remain current independently.

On a retained legacy statement, **Assign cycle and recheck** reuses the original
file, preserving its date and checksum. This is restricted to scoped finance or
unit administrators. A file referenced by retained payment evidence cannot be
reclassified. An already confirmed cycle cannot be rewritten using this action.

When a subsequently captured invoice has an exact eligible row in its current
cycle's file, reconciliation runs automatically and is audited as automatic.
Duplicates, amount/account/currency differences, post-dated invoices, wrong-unit
assignments and cycle ambiguity are not eligible. Reconciliation does **not**
approve, submit or pay an invoice; normal review, anomaly clearance, independent
signing and fresh pre-settlement checks remain mandatory.

## Read the calendar

- **Received:** recorded invoices, including cancelled records.
- **Processed (paid):** invoices whose recorded status is Paid.
- **Pending:** recorded invoices that are neither Paid nor Cancelled.
- **Overdue:** pending invoices past their due dates.

Click a cycle's count to open the matching records, then open the invoice or
retained source. Counts respect the user's ministry access. Amounts remain in
their original currencies; calendar counts do not combine monetary totals.
These are recorded-invoice counts, not estimates of bills not yet received.

Historical records are initially labelled **Awaiting cycle assignment** rather
than inventing past cycle evidence. Their financial snapshots, signatures,
source documents and payment history are unchanged.

## Verification

The full regression run passed 533 tests. A focused follow-up covering cycles,
bulk intake, batch preparation and reconciliation passed 58 tests, including
additional checks that cycle changes invalidate stale matches without changing
the financial snapshot.

A signed-in browser journey created two separate CEB cycles with identical
October dates and loaded their files independently. A later utility-record
upload was captured once, classified to the correct cycle and automatically
reconciled. It remained a draft with no approval or payment. Calendar counts,
pending-invoice drill-downs and retained-source links were checked at desktop
and mobile widths. Only labelled demo records were added; no external payments
or messages were sent.
