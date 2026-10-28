# GovBills — Government Utility Invoice Management (Republic of Mauritius)

GovBills is for ministries and their departments, hospitals, schools and other sites. It handles the bills from
utility providers such as the Central Electricity Board (CEB), Central Water Authority (CWA), Mauritius Telecom,
Emtel and the Wastewater Management Authority (WMA). With it, staff can:

* **capture** invoices from PDFs, scans, phone photos, Excel/CSV statements or provider APIs, with OCR deciding
  where on the page each value is printed;
* **approve** them through SLA-bound, digitally signed workflows that always finish before the due date;
* **instruct payment** with signed settlement instructions that total the amount owed to each utility, plus ISO 20022
  bank files;
* **analyse** spend and consumption trends for every ministry, unit, provider and meter.

It works in Mauritian rupees (MUR, 2 decimals) and the Port Louis time zone. SLA clocks follow a Monday–Friday civil
service week (09:00–16:00) and skip Mauritian public holidays. Bank accounts use MU IBANs, OCR reads English and French
invoices, and the look follows the agreed template: a royal-blue gradient, a floating pill navigation bar, Poppins
type and glass KPI tiles.

## Architecture

```
            Browser (staff)                Other systems / AI assistants
                  │  HTTPS, session + TOTP 2FA        │  HTTPS, Bearer token
          ┌───────▼────────────────────────────────────▼────────┐
          │                Django application                    │
          │  UI · workflow · OCR · signatures · settlements      │
          │  ┌──────────────── Integration gateway ───────────┐  │
          │  │ REST API /api/v1 · MCP /mcp · SMTP · SMS       │  │
          │  └────────────────────────────────────────────────┘  │
          │  Admin console /console (settings, health, logs)    │
          └──────┬───────────────┬──────────────┬───────────────┘
                 │               │              │
        PostgreSQL (backend   /data volume    Redis (shared cache,
        database: all records, (documents,    API rate limits)
        audit chain, config,   signed PDFs,
        integration logs)      OCR previews, CA)
                 ▲
     scheduler worker (SLA, alerts) · OCR worker (large uploads)
```

## Capabilities

| Area | What it does |
|---|---|
| **Organisation** | Government → ministry → directorate/department → site. A role on a unit applies to everything beneath it, and delegations can expire. The demo includes the Prime Minister's Office, Ministry of Health and Wellness, Ministry of Education and Human Resource and Ministry of Finance, with real hospitals and school zones. |
| **OCR & document intelligence** | Born-digital PDFs are read from their text layer; scans and photos (PNG/JPG/TIFF/scanned PDF) go through Tesseract (English + French). Pages are rotated to the right orientation, deskewed, denoised and contrast-corrected first. The analyser **identifies the provider** (header name, aliases, tax number, IBAN, registered account numbers) and then **locates each field**: account, invoice number, dates, billing period, net/VAT/total, consumption and meter readings. It uses English/French labels (value to the right of or below the label), the provider's zone template, and the account registry. Cross-checks (net + VAT = total, readings = consumption, plausible payment terms) adjust confidence. The review screen draws a box around every value on the page image with its confidence. Required fields under the threshold must be **confirmed by a person** before import. Each confirmation **teaches the provider's template**, with zones anchored to the nearby printed label so shifted scans still line up. Admins can also draw zones in the template editor. |
| **Mauritian bill formats** | Built-in reading profiles for the **Mauritius Telecom statement of account** (VAT20070778) and the **CEB e-Bill** (VAT22000591), plus the **MT summary CSV** export (semicolon-separated, "September 2026" periods). They pick out:<ul><li>the billing account rather than the customer account (MT *Bill. Acc.*, CEB *Contract Acct.*), matched even with leading zeros, or through the line / meter number;</li><li>the amount of *this* bill (MT *Total amount inclusive of VAT*, CEB *Current amount payable (B)*), kept apart from arrears, which are captured and flagged;</li><li>dd.mm.yy dates, billing periods, meter readings and consumption.</li></ul>MT is recognised from its FINV bill-number format even in files that don't name it. Zero-amount bills (e.g. executive discount) are flagged and closed as "nothing to pay" once approved, without a payment. Unregistered accounts get a one-click **Register account** link and **Re-check all rows**. Admins can add patterns for other providers (`OcrTemplate.patterns`), and `manage.py apply_provider_profiles` fills in the VAT numbers and bill-number formats. |
| **Invoice intake** | Manual entry, Excel/CSV with column recognition, OCR documents, and the provider e-invoicing API. Every invoice keeps its original document and SHA-256 hash. Duplicates are blocked, and the system flags anomalies: spikes against the account's history, total mismatches, wrong readings, overlapping periods, reused documents and late arrivals. |
| **Approval workflow** | Policies are set by unit, utility and amount band, with steps defining the role, the SLA in working hours, whether a signature is required, and value thresholds (for example, a senior approval above Rs 500,000). Each step's deadline is capped at *must-approve-by*, which is the due date minus the settlement lead days. Late invoices are expedited. Reminders go out at 75% of the time used; on breach the step is escalated and the escalation role may decide it. Steps can be returned or disputed and then resubmitted as a new round. |
| **Segregation of duties** | Enforced in the workflow engine itself. Whoever records or submits an invoice can't approve it, nobody decides two steps of the same round, and the preparer of a settlement can't authorise it. Changing a provider's bank details needs two administrators. |
| **Digital signatures** | X.509 identities come from the internal CA or are imported from national PKI PKCS#12 files. The key is protected by a PIN and by server encryption, with a PIN lockout. Approval decisions are signed over the invoice figures and document hash. Settlement PDFs carry PAdES signatures from preparer and authoriser (B-T with a timestamp authority). A verification page checks any signed PDF. |
| **Settlement** | Approved invoices are totalled **per utility provider**. The pay-by date is the earliest due date and execution is requested one working day earlier. The output is a signed PDF, an ISO 20022 pain.001.001.09 file and a CSV. Recording execution marks the invoices paid. |
| **Analytics** | Monthly spend by utility, a consumption index, spend by unit with drill-down, year-on-year comparison, a seasonal projection, top providers and accounts, SLA by step, payment timeliness, anomalies and 24-month account histories, with CSV export. |
| **Integration gateway** | **REST API** `/api/v1`: invoices (read and submit), dues, providers, settlements and analytics, with an OpenAPI description. **MCP server** `/mcp`: JSON-RPC over HTTP (Streamable HTTP, protocol 2025-06-18) with read-only tools (search_invoices, get_invoice, upcoming_dues, sla_breaches, spend_summary, list_settlements), so AI assistants can answer questions but never approve, sign or pay. Clients get hashed bearer tokens shown only once, scopes, an organisation-unit restriction, an IP allowlist, a per-minute rate limit, an expiry date and a full request log. **SMTP**: e-mail through the configured relay (STARTTLS/SSL) with a delivery log. **SMS**: a generic HTTPS/JSON gateway (local operators) or Twilio. Critical alerts (SLA breaches, payment deadlines) go to users' mobiles, and numbers are masked in logs. |
| **Admin console** `/console` | **System health:** database engine, version, size, connections and pending migrations, document storage and disk, scheduler heartbeat, OCR engine, languages and queue, signing-CA expiry, audit-chain verification, and integration traffic and errors. **Settings:** business rules, working calendar, payer account, OCR, security limits and branding, stored in the database and applied to every server process within seconds. Secrets are encrypted and never shown again. **Also:** SMTP and SMS setup with test sends, API/MCP client management, integration logs, public holidays, and a link to low-level data administration. |
| **Security** | Mandatory TOTP two-factor login with recovery codes and replay protection. Lockout, Argon2 hashing, a strong password policy, idle timeout and session limits. Strict CSP with no inline script or style (fonts are self-hosted), HSTS and `__Host-` cookies. Uploads are checked by content and documents are served only after permission checks. Bank details, TOTP secrets, signing keys and console secrets are encrypted. The audit log is hash-chained. |

## Quick start (development)

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

OCR needs Tesseract with English and French language data. On Debian/Ubuntu:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra
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

Open http://localhost:8000. Every demo user has the password `Demo-Passw0rd-2026!` and the signing PIN `Sign-2026!`.
They all share the TOTP secret `JBSWY3DPEHPK3PXP`; add it to an authenticator app as a manual key.

| User | Role |
|---|---|
| `sysadmin` | System administrator (admin console) |
| `h.clerk` | Accounts officer, Ministry of Health and Wellness (has a staged OCR batch to review) |
| `s.verifier` / `m.approver` / `k.director` | Health verification, budget-holder and senior approval |
| `f.finance` / `a.authoriser` | Prepares / authorises settlement instructions (Health, Education) |
| `e.clerk` / `e.verifier` / `e.approver` | Same roles for Education and the Prime Minister's Office |
| `g.admin` | Government-level administrator; `auditor` has read-only audit access |

The seed also prints a demo API/MCP token. Try it:

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/dues?days=30
```

```bash
curl -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' http://localhost:8000/mcp
```

Run the tests (102 tests; the OCR tests need Tesseract):

```bash
.venv/bin/pytest
```

## Production deployment

1. Copy the environment template, then set the secrets in `.env`. The app refuses to start without
   `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` and `SIGNING_CA_PASSPHRASE`.

   ```bash
   cp .env.example .env
   ```

2. Start the stack. This runs **PostgreSQL** (the backend database), the web app, the **scheduler**, the **OCR
   worker** and **Redis**.

   ```bash
   docker compose up -d --build
   ```

3. Terminate TLS in front of port 8000 and set `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `SITE_URL`.
4. Create the first system administrator:

   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

5. Sign in, enrol two-factor login, then open **Security → Admin console**:
   * configure SMTP and SMS and send test messages;
   * set the payer account, working calendar and holidays;
   * create API/MCP clients.

   Then build the unit tree, roles, providers (with bank details under dual control) and service accounts.
6. Back up PostgreSQL (`pg_dump`) **and** the `/data` volume. Keep `.env` in the secrets vault: without
   `FIELD_ENCRYPTION_KEYS`, encrypted data can't be read.

### Operations

* `python manage.py verify_audit_chain` exits non-zero if any audit entry was altered or removed. Schedule it.
* `python manage.py run_scheduler --loop 300` handles SLA reminders and escalation, due-date alerts, overdue flags and
  the OCR queue fallback. The console shows its heartbeat.
* `python manage.py ocr_worker --loop 5` analyses uploads with more than `OCR_SYNC_MAX_FILES` files in the background.
* To rotate the field-encryption key, prepend a new key to `FIELD_ENCRYPTION_KEYS`.
* To use the national PKI, add its root to `SIGNING_EXTRA_TRUST_ROOTS`; set `SIGNING_TSA_URL` for trusted timestamps.
* Add each year's moving public holidays (Thaipoosam Cavadee, Chinese Spring Festival, Maha Shivaratree, Ougadi,
  Eid-ul-Fitr, Ganesh Chaturthi, Divali) in **Console → Holidays** once they are gazetted.

## Project layout

```
config/        settings (environment), URLs
core/          audit chain, encryption, money/currency precision, working-time calendar, notifications, dashboard
accounts/      users, TOTP 2FA, lockout, idle timeout, user administration
org/           government structure, roles, row-level access (org/access.py)
providers/     utility providers, service accounts/meters, dual-control bank changes
invoices/      invoice model, intake rules, anomaly detection
imports/       spreadsheet parsing, staged review, OCR document intake, confirmation gate
ocr/           engine (Tesseract / text layer, clean-up), field analysis (extract.py), templates & learning, review UI
workflow/      approval policies and engine, SLA sweep, scheduler
signatures/    internal CA, signing identities, detached and PAdES signatures, verification
settlements/   instructions, PDF, ISO 20022 export
analytics/     trend queries and analysis page
integrations/  runtime config, REST API, MCP server, SMTP backend, SMS gateway, admin console
tests/         102 tests: security, workflow, signatures, settlement, imports, OCR, MT/CEB formats, gateway, console
```

## Notes

* **Data to confirm with the government.** The demo ministries, sites, providers and VAT treatment (electricity,
  water and wastewater at 0%, telecom at 15%) are seed data. Check them against the current Government Gazette and
  each provider's actual billing before go-live; all of them can be edited in the app.
* **Branding.** It uses the product name *GovBills* (`BRAND_NAME` / `BRAND_HIGHLIGHT`) and follows the reference
  template's colours and layout, but deliberately doesn't copy another organisation's logo. Set your own name in the
  console.
* **Relationship to InvoiceShelf.** This system was assessed against InvoiceShelf 3.x (alpha, AGPL-3.0, PHP 8.4). No
  InvoiceShelf code is included; its concepts were reimplemented for the government accounts-payable setting.
* **Not included:**
  * single sign-on (SAML/OIDC) with the government identity provider;
  * handwriting recognition;
  * write actions over MCP (excluded by design).
* **Before production:** have the system independently penetration-tested and the CA procedures reviewed.
