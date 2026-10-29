# Settlement groups

Finance can open **Settlements & groups → Group approved batches**, choose a
paying unit, utility provider and currency, then select complete approved
batches. Select-all groups all eligible batches shown for that selection.

The resulting draft settlement instruction has its own `SG` group reference.
It is a pre-payment grouping: creating it does not authorise, transmit or execute
a bank payment. Existing preparer signing, independent authorisation, release
checks and post-settlement bank reconciliation still apply.

## Controls

- One provider, one ministry and one currency per group.
- Every payable member must be approved, reconciled, on an active service account,
  and clear of unresolved disputes and surcharge-review holds.
- No partial batches. Approved zero-payable members already closed as paid remain
  in their original batch but do not add a payment item.
- Live settlement instructions reserve their member invoices and batches.
- The creation transaction rechecks the complete selection. A stale or invalid
  batch rejects the entire group rather than paying only the remaining members.
- Group membership, per-batch totals and per-invoice batch attribution are
  retained. To change a draft/pending group, cancel it and create another; do not
  modify the original record.

## Where to inspect the group

- Settlement register: **Settlement groups** filter and `SG` reference.
- Instruction detail: linked member batches, invoice counts, payable snapshots,
  combined total and full instruction audit history.
- Processing batch detail: linked settlement groups and lifecycle events from
  their shared instruction, including cancellation and execution.
- Signed instruction PDF: group reference and per-batch payable summary.
- Derived invoice approval copy: original processing batch and settlement group.

Cancellation releases the invoices for another settlement but preserves the
original group and audit history. Post-payment disputes and refunds must not
rewrite the group amount or original signed payment evidence.
