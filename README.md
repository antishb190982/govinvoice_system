# GovBills — Government Utility Invoice Management

GovBills lets ministries and their sub-units record, approve and settle utility invoices (electricity, water, gas,
telecom, internet, waste, fuel, district cooling) on time. It also shows how each invoice, account, unit and provider
trends over time.

Approvals run against SLAs measured in working hours and never extend past the date an invoice must be settled.
Approvals and payment instructions carry digital signatures. Every action is recorded in a tamper-evident audit trail.

## Main capabilities

| Area | What it does |
|---|---|
| **Organisation** | Unlimited tree: government → ministry/authority → directorate → department → site. A role granted on a unit applies to everything beneath it, and nothing sideways or upwards. Delegations can have an expiry date. |
| **Providers & accounts** | Provider master data with bank details encrypted at rest. Changing bank details needs **dual control**: one administrator requests the change, a different one approves it, and IBAN checksums are validated. Service accounts (meters) are owned by units. |
| **Invoice intake** | Manual entry, **Excel (XLSX/XLS) and CSV statements** with automatic column recognition or per-provider templates, and **PDF invoices** with text extraction. Imports go to a review screen where you can fix, skip or commit rows. Each invoice keeps the original document and its SHA-256 hash. |
| **Integrity checks** | Duplicate provider invoice numbers are blocked. The system also flags: possible duplicates (same account, period and amount), the same file reused, net + tax ≠ total, meter readings that don't add up, overlapping billing periods, amount or consumption spikes against the account's recent history, invoices arriving too late for the normal SLA, and overdue invoices. |
| **Approval workflow** | Configurable policies by unit, utility and amount band, each with ordered steps (role, SLA in working hours, signature required, extra steps from a threshold amount). Each step's SLA deadline is capped at the invoice's *must-approve-by* time (due date minus `SETTLEMENT_LEAD_DAYS` working days). Late invoices are expedited automatically. Steps can be returned for correction or disputed with the provider, then resubmitted as a new round. |
| **SLA control** | Reminders go out at 75% of the time used. On breach, the step is flagged and escalated: the escalation role (for example the unit administrator) is notified and may decide the step itself. The SLA monitor and dashboard show compliance. Working week, hours and public holidays are configurable. |
| **Segregation of duties** | Enforced in the engine, not only the UI. Whoever records or submits an invoice cannot approve it, nobody decides two steps of the same approval round, and the preparer of a settlement instruction cannot authorise it. |
| **Digital signatures** | Each user has an X.509 signing certificate, issued by the built-in CA or **imported from the national PKI** as PKCS#12. The key is encrypted with the user's signing PIN and again with the server key. Repeated wrong PINs lock signing. Approval decisions are signed over the invoice figures and document hash, and any later change shows up as an invalid signature. Settlement PDFs carry **PAdES** signatures from the preparer and the authoriser (visible stamps, and B-T timestamps when a TSA is configured). A verification page checks any signed PDF. |
| **Settlement instructions** | Collects approved invoices due by a cut-off date, totals them **per utility provider**, sets the pay-by date (earliest due date) and the requested execution date (one working day earlier). It produces a signed PDF, an **ISO 20022 pain.001.001.09** file and a CSV. Marking an instruction executed records the bank reference and pays the invoices. Finance is alerted about approved invoices close to their due date that aren't in an instruction yet. |
| **Trend analysis** | Monthly spend by utility, consumption indexed per utility, spend by unit (drill down by choosing a unit), year-on-year comparison, next-month projection with seasonality, top providers and costliest accounts, approval SLA by step, payment timeliness and anomaly lists. Every account has a 24-month chart, and the data exports to CSV. |
| **Security** | Mandatory TOTP two-factor login with single-use recovery codes and replay protection. Account lockout and IP throttling, Argon2 password hashing, a strong password policy and forced change of temporary passwords. 15-minute idle timeout and 8-hour session limit. Strict CSP with no inline scripts or styles, plus HSTS, secure SameSite=Strict cookies, `no-store` caching and clickjacking protection. Uploads are checked by content, files are served only through permission checks, and CSV exports are protected against formula injection. The audit log is **hash-chained**. |

## Roles

| Role | Typical holder | Can |
|---|---|---|
| Viewer | Managers | See invoices, dashboards and trends in their units |
| Invoice clerk | Accounts staff | Record and import invoices, submit and resubmit them |
| Verifier | Facilities / engineering | Check consumption and account details (first step) |
| Approver | Budget holders | Approve, with a digital signature |
| Finance officer | Finance | Prepare and sign settlement instructions, export payment files |
| Payment authoriser | Head of finance | Counter-sign and authorise instructions |
| Unit administrator | Ministry admin | Manage sub-units, service accounts and users. The escalation target for SLA breaches |
| Auditor | Internal audit | Read-only access, audit-trail viewing and chain verification |
| System administrator | ICT | Configuration (policies, holidays, import templates) |

## Quick start (development)

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python manage.py migrate
```

```bash
.venv/bin/python manage.py seed_demo
```

```bash
.venv/bin/python manage.py runserver
```

Open http://localhost:8000. Demo users (password `Demo-Passw0rd-2026!`, signing PIN `Sign-2026!`) share the TOTP
secret `JBSWY3DPEHPK3PXP`. Add it to an authenticator app as a manual key. Each demo user has a different job:

* `h.clerk` records and imports invoices for the Ministry of Health (MOH).
* `s.verifier` is the first approval step for MOH.
* `m.approver` signs approvals for MOH.
* `k.director` is the MOH senior approver and unit administrator.
* `f.finance` prepares settlement instructions for MOH and the Ministry of Education (MOE).
* `a.authoriser` authorises them.
* `e.clerk`, `e.verifier` and `e.approver` play the same roles for MOE and the Ministry of Interior (MOI).
* `g.admin` is the government-level administrator, `auditor` has audit access, and `sysadmin` is the system administrator.

Run the tests:

```bash
.venv/bin/pytest
```

The SLA reminders, escalations and due-date alerts come from one scheduled command. Run it every 5–15 minutes, from
cron, a systemd timer or the `scheduler` container:

```bash
python manage.py run_scheduler
```

## Production deployment

1. Copy the environment template and set every secret in `.env`:

   ```bash
   cp .env.example .env
   ```

   The app refuses to start in production without `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` and
   `SIGNING_CA_PASSPHRASE`.
2. Start the stack (PostgreSQL, the web app and the scheduler worker):

   ```bash
   docker compose up -d --build
   ```

3. Terminate TLS in front of port 8000 (the agency load balancer or nginx). Set `DJANGO_ALLOWED_HOSTS`,
   `DJANGO_CSRF_TRUSTED_ORIGINS` and `SITE_URL` to the public HTTPS name.
4. Create the first system administrator:

   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

   Then sign in, enrol two-factor login and build the unit tree, roles, providers and service accounts. The approval
   policy, holidays and import templates are configured under *System configuration*.
5. Back up the PostgreSQL database **and** the `/data` volume, which holds uploaded documents, signed PDFs and the CA.
   Keep `.env` in your secrets vault: without `FIELD_ENCRYPTION_KEYS`, bank details and signing keys cannot be decrypted.

### Operations

* `python manage.py verify_audit_chain` exits non-zero if any audit entry was altered or removed. Schedule it and
  alert on failure.
* To rotate the field-encryption key, prepend a new key to `FIELD_ENCRYPTION_KEYS`. Old keys still decrypt existing
  data.
* To use the national PKI, add its root to `SIGNING_EXTRA_TRUST_ROOTS` and have staff import their PKCS#12
  certificates. Set `SIGNING_TSA_URL` to get trusted timestamps on PDFs.
* Read [docs/SECURITY.md](docs/SECURITY.md) before go-live.

## Project layout

```
config/       settings (all from environment), URLs
core/         audit chain, encryption, working-time calendar, notifications, upload checks, dashboard
accounts/     users, two-factor login, lockout, idle timeout, user administration
org/          organisation tree, roles, row-level access rules (org/access.py)
providers/    providers, service accounts, dual-control bank changes
invoices/     invoice model, intake rules and anomaly detection (invoices/services.py)
imports/      spreadsheet/PDF parsers and staged import review
workflow/     approval policies, engine (workflow/engine.py), SLA sweep, scheduler command
signatures/   internal CA, signing identities, detached and PAdES signatures, verification
settlements/  instructions, PDF rendering, ISO 20022 export
analytics/    trend queries and analysis page
tests/        68 tests covering security controls, workflow rules, signatures, settlement and imports
```

## Relationship to InvoiceShelf

This system was assessed against InvoiceShelf 3.x. That codebase is an alpha-stage invoicing tool for small
businesses, built on PHP 8.4 / Laravel 13 under the AGPL-3.0 licence. It has no approval workflow, SLA handling,
signatures, ministry hierarchy or import pipeline. Its useful concepts (supplier bills, payment allocation, due-date
sweeps, per-company scoping) were reimplemented here for the government accounts-payable setting. **No InvoiceShelf
code is included**, so its AGPL obligations do not apply to this codebase.

## Known limitations / next steps

* **Scanned (image-only) PDFs** are rejected for manual entry. Add OCR (e.g. Tesseract) if providers send scans.
* **Single sign-on** (SAML/OIDC against the government identity provider) is not included. TOTP two-factor login is
  built in.
* **E-mail is sent synchronously after commit.** For high volume, move it to a task queue.
* **Provider e-invoicing API intake** (UBL / provider portals) is designed for but not implemented: invoices have a
  `source = api` value ready for it.
* **Before production:** have the code independently penetration-tested and the internal CA procedures reviewed
  against national PKI policy.
