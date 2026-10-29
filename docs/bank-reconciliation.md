# Bank reconciliation after settlement

Open **Finance → Bank reconciliation**. This is separate from **Invoice
reconciliation**, which remains mandatory before approval and payment.

## Import

Finance staff or unit administrators upload a UTF-8 CSV using the downloadable
template. Select the finance unit, currency, statement date and a stable, unique
bank-account alias. Do not use a full bank account number as the alias.
The original private file, SHA-256, uploader and parsed rows are retained.
Limits: 10 MB and 10,000 rows.

Required columns:
`transaction_id,settlement_reference,amount,currency,value_date`

Optional columns: `provider_code,bank_reference`.

- IDs are text; preserve leading zeroes. Dates use `YYYY-MM-DD`.
- Amounts are positive debits with a dot decimal separator; quote thousands
  separators if using them in a comma-separated file.
- `settlement_reference` is the GovBills settlement-instruction reference.
- Blank `provider_code` matches the entire batch debit. A provider code matches
  exactly that beneficiary transfer. Multiple provider debits can therefore
  reconcile a batch progressively.
- An optional `bank_reference`, when supplied, must agree with the execution
  reference recorded on the instruction.
- The instruction must already be **Executed**; uploaded bank data never sends a
  payment or changes an invoice's Paid status.

## Controls and exceptions

Exact amount and currency matching is mandatory. Unknown/out-of-scope
instructions, non-executed payments, incorrect amounts/references, invalid/future
dates and credits are exceptions, not forced matches. Fees, credits, reversals,
opening/closing balances and foreign-exchange conversion are not automatically
posted. This feature reconciles settlement debits, not a general ledger or
whole-account period close.

Value dates normally equal the recorded execution date. A different finance
staff member or unit administrator can acknowledge a timing difference with a
required reason. The uploader cannot acknowledge their own date variance.
This cannot waive amount, currency, reference, execution-state or duplicate checks.

Reusing a bank-account alias and transaction ID cannot allocate the debit twice.
Exact reuploads are labelled previously reconciled; conflicting replays are
exceptions. Duplicate transaction IDs within a single file are all blocked.
Each beneficiary transfer can have only one allocation, including when a batch
was matched as one debit. Use the same account alias consistently across imports.

Recheck compares current settlement evidence. Changes to execution details
invalidate the bank check; rechecking releases stale allocations with an audit
event. Source rows are never edited. All matching/review decisions are audited.

Finance, unit administrators, payment authorisers and auditors can read within
their unit scope. Only finance/unit administrators can import or recheck.
The private source download enforces the same scope.
