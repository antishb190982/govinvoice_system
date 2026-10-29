# GovBills: receipt-to-settlement journey

## Operational sequence

1. **Receive — clerk or trusted mail relay.** Upload files, a complete folder or ZIP, or stage authenticated e-bills. Dedicated mailbox addresses and exact sender-to-provider rules are maintained under **Admin → Gateway centre → Incoming e-bills**.
2. **Check and register — processor and unit administrator.** Reuse the provider template, check extracted invoice values, register missing accounts through the responsible unit administrator, and assess receipt dates, due dates and surcharges.
3. **Reconcile — the authorised reconciliation team.** Match the provider's pending-invoice XLS/CSV against accounts, invoice references and amounts. Resolve mismatches in the Unsorted basket.
4. **Prepare — ministry processor.** Prepare batches containing reconciled invoices for a single utility provider, within the user's authorised ministry scope.
5. **Review and approve — assigned workflow roles.** Approval operates on batches, not individual invoices. Preserve decisions, signatures, dates and stamped operational copies separately from originals.
6. **Group and authorise — finance and an independent authoriser.** Approved batches of the same provider may be grouped for settlement. Retain each batch's identity and obtain independent authorisation.
7. **Settle — finance.** Validate approval and reconciliation evidence again, record execution and bank references, and retain the original financial evidence and batch amounts.
8. **Match and inspect — bank reconciler and auditor.** Reconcile the bank statement against settlement references. Assign discrepancies and inspect the batch, invoice and settlement history.

## Visibility and operational benefits

- My Space and My Work bring scoped responsibilities and assigned work together.
- The header bell shows live overdue work and recorded alerts without repeating a large banner on every page. Counts are work items, not necessarily unique invoices.
- Ministry timeliness reporting compares recorded receipt/submission dates with due dates and shows billed surcharges, recurring delays, data gaps and invoice-level evidence.
- Reporting is permission-scoped and currency-specific. Recorded delay and surcharge associations do not establish who caused a charge.
- Provider template reuse reduces repeated mapping. Reconciliation gates and independent authorisation remain in place; no time-saving percentages are claimed.

## Mail setup without an Outlook integration

No Outlook account is connected by saving a mailbox address. A mail administrator must connect the real dedicated mailbox to the secure relay endpoint `/api/v1/mail/receive` over HTTPS.

Create a single-purpose **Receive e-bills** API client and bind it to the mailbox. Select an active, non-superuser clerk/unit-administrator processing identity whose capture permissions fit within the client's organisational scope. Keep intake disabled until the external relay is configured.

The relay must verify aligned sender authenticity, scan attachments, supply an integrity-bound original RFC822 message and preserve the actual mailbox receipt time. Sender-controlled `Date` and `Authentication-Results` headers are not trusted proof.

Unmapped and unauthenticated senders are rejected. Missing clean-scan evidence quarantines the delivery before invoice attachment parsing. Successful deliveries stage invoice records for normal checks; they never automatically approve, authorise or pay bills. Exact delivery replays are acknowledged without duplicate staging.

## Rollout conditions

Harden temporary shared verification settings, configure notification gateways and the actual mail relay, verify backup/recovery and access boundaries, and use approved production records. Presentation captures are demo evidence and may show earlier interface styling. They do not demonstrate live bank transfers.
