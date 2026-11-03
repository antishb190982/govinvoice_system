# GovBills
## On-site deployment, code, dependencies and security

Technical implementation guide | 3 November 2026

Source: antishb190982/govinvoice_system, branch govbills-mauritius.
Audience: infrastructure, application support, security, database and implementation teams.

This guide describes a proposed Linux VM or physical-server deployment of the standalone Django portal. It is based on the inspected source, not a completed installation on your premises. Commands and service samples must be qualified against the organisation's operating system, DNS, certificates, network and identity policies.

IMPORTANT: The current published environment is a demonstration. Temporary shared verification is enabled in the Replit adapters, and fictional walkthrough examples are not real approvals, payments, bank confirmations or financial reporting records. Do not promote these settings or records into live operations.

## 1. Deployment boundary and architecture

Deploy the standalone repository with manage.py at its root. In Replit, the same source is inside artifacts/govbills/server. Preserve that distinction: the Replit multi-artifact workspace and its deployment wrapper are not the on-site deployment entry point.

Use config.settings and config.wsgi.application on site. Do not use config.replit_preview, prepare_replit_preview or the Replit-only production adapter as an on-site baseline. The Replit adapter depends on managed secrets, routing and App Storage; the standalone settings support PostgreSQL and private filesystem storage.

```
Staff browser -- HTTPS --> Nginx / approved reverse proxy
                              |
                       loopback Gunicorn
                              |
                         Django portal
                  /          |           \
             PostgreSQL    Redis     private documents
                              \
                 OCR, signing, scheduler and gateways
```

Separate the application service, database, shared cache, private documents and signing-key storage by service identity and access policy. Keep the database, Redis, private files and Gunicorn port off public networks.

The on-site scope is the Django staff portal. The Replit slide editor, mockup sandbox and companion Node API are separate workspace artifacts and are not required to run the standalone Django application.

Sizing is not benchmarked. A starting evaluation host of 4 vCPU and 8-16 GB RAM is a planning assumption, not a capacity commitment. Test real PDF sizes, concurrent staff, folder imports and OCR bursts before approving production sizing. Run scheduler processing as a separate supervised service.

## 2. Runtime and dependency inventory

The workspace declares Python 3.13 or later. Use an approved Python 3.13 installation for initial qualification. Pin application dependencies from requirements.txt; do not replace the pinned file with an unconstrained latest-package install.

| Component | Inspected version / dependency | Purpose |
| --- | --- | --- |
| Web framework | Django 5.2.18 | Portal, forms, ORM, security middleware |
| WSGI process | Gunicorn 26.2.0 | Production HTTP application workers |
| Database driver | psycopg[binary] 3.3.6 | PostgreSQL connectivity |
| Password hashing | argon2-cffi 25.1.0 | Argon2 password hashes |
| Encryption | cryptography 50.0.2 | Encrypted sensitive fields and signing material |
| PDF signing | pyHanko 0.38.0; pyhanko-certvalidator 0.33.0 | Signed PDF evidence and certificate validation |
| Generated documents | reportlab 5.1.0; pypdfium2 5.14.0 | PDF output and PDF rasterisation |
| Invoice text | pdfplumber 0.11.10; pytesseract 0.3.13 | Text extraction and OCR adapter |
| OCR executable | Tesseract with eng, fra and orientation data | Native OCR; not installed by pip |
| Image / numeric processing | Pillow 13.0.0; numpy 2.5.4 | Image preprocessing and analysis |
| Spreadsheets | openpyxl 3.1.5; xlrd 2.0.2 | XLSX and legacy XLS intake |
| Second factor | PyOTP 2.10.0; qrcode 8.2 | Authenticator enrolment and verification |
| Static assets | WhiteNoise 6.12.0 | Compressed hashed static assets in production |
| Shared cache client | redis 8.1.0 | Optional Redis integration; recommended for multiple workers |
| HTTP/TLS baseline | aiohttp 3.14.5; certifi 2026.10.23; idna 3.20 | Explicit patched standalone dependency constraints |
| Replit storage adapter | replit-object-storage 1.0.2 | Used by Replit-specific deployment, not required as the on-site storage design |

Also install a supported PostgreSQL server, a supported Redis server where enabled, Nginx or an equivalent approved proxy, trusted CA certificates, time synchronisation, and a font suitable for any required local-language PDF output. Test native wheels on the selected CPU architecture.

Example native OCR installation on Debian/Ubuntu, after the platform team has provisioned Python:

```
sudo apt-get install tesseract-ocr tesseract-ocr-eng \
  tesseract-ocr-fra tesseract-ocr-osd
tesseract --list-langs
```

requirements-dev.txt adds developer/test tools and inherits the runtime file. Node/pnpm is required for the wider Replit workspace and presentation tooling, not as the standalone Django request runtime.

The documented dependency audit on 3 November 2026 reported zero known findings after remediation. This is a dated package-advisory result, not a penetration-test certificate or proof that all application vulnerabilities are absent. Re-run an approved audit for each release.

## 3. Installation and secure configuration

Create a dedicated non-root service account, approved installation directories and restricted runtime directories. The paths below are examples. Keep application code read-only to the runtime account where your release process allows it.

```
/srv/govbills/current          # deployed repository/release
/srv/govbills/venv             # dedicated on-site Python environment
/etc/govbills/govbills.env      # restricted runtime configuration
/var/lib/govbills/private      # private source and generated documents
/var/lib/govbills/ca           # encrypted signing CA material
```

A dedicated Python environment is an on-site host recommendation, not a change to the Replit workspace environment.

```
git clone --branch govbills-mauritius \
  https://github.com/antishb190982/govinvoice_system.git \
  /srv/govbills/current
python3.13 -m venv /srv/govbills/venv
/srv/govbills/venv/bin/python -m pip install \
  -r /srv/govbills/current/requirements.txt
/srv/govbills/venv/bin/python -m pip check
```

Provision the PostgreSQL database and least-privilege roles through the database team. Use a separate controlled migration role where required. Normal application operation does not require a PostgreSQL superuser.

Required configuration and important differences:

| Setting | On-site requirement |
| --- | --- |
| GOVBILLS_ENV | production; this activates production checks and security settings |
| DJANGO_SETTINGS_MODULE | config.settings, not a Replit adapter |
| DJANGO_DEBUG | false |
| DJANGO_SECRET_KEY | Unique high-entropy value held in the approved secret store |
| FIELD_ENCRYPTION_KEYS | Comma-separated Fernet keys; first encrypts, retained keys decrypt |
| SIGNING_CA_PASSPHRASE | Independent secret protecting signing CA material |
| DJANGO_ALLOWED_HOSTS | Explicit hostnames; do not use a wildcard |
| DJANGO_CSRF_TRUSTED_ORIGINS | Exact approved HTTPS origins |
| POSTGRES_DB / USER / PASSWORD / HOST / PORT | Standalone settings use these names; DATABASE_URL alone is not the standalone PostgreSQL selector |
| POSTGRES_SSLMODE | Set according to the database TLS policy; qualify verify-full and trust roots |
| PRIVATE_STORAGE_ROOT | Persistent private filesystem location outside the web root |
| SIGNING_CA_DIR | Restricted persistent CA directory, backed up separately and securely |
| REDIS_URL | Shared Redis endpoint where enabled; restrict network and credentials |
| ADMIN_URL_PATH | Organisation-selected administrator URL path |
| EMAIL_* / gateway settings | Approved SMTP/TLS and optional SMS connection, configured separately |

The standalone application falls back to SQLite when POSTGRES_DB is absent. An on-site live release must explicitly reject that fallback during acceptance. The standard settings do not automatically enforce every infrastructure policy in this guide.

Keep the environment file owned by root and readable only by the authorised service group, for example mode 0640. Do not put secrets in Git, tickets, deployment PDFs, shell history, screenshots or test fixtures. Do not reuse demo credentials.

## 4. First deployment, migration and services

Load the approved environment in the controlled release session before the following commands. Execute them from the repository root with the correct service identity and database privileges.

```
cd /srv/govbills/current
/srv/govbills/venv/bin/python manage.py check --deploy
/srv/govbills/venv/bin/python manage.py migrate --noinput
/srv/govbills/venv/bin/python manage.py collectstatic --noinput
/srv/govbills/venv/bin/python manage.py setup_govbills
/srv/govbills/venv/bin/python manage.py createsuperuser
```

These are on-site migration steps. They are not instructions to run schema DDL against the existing Replit production database. Replit-managed publishing uses its own development-to-production schema process.

Create the initial administrator through the controlled command, enrol an individual authenticator and establish named staff roles. The Replit browser bootstrap/setup-token flow is not the baseline on-site enrolment process.

Example systemd application service, to be adapted and reviewed:

```
[Unit]
Description=GovBills Django portal
After=network-online.target

[Service]
User=govbills
Group=govbills
WorkingDirectory=/srv/govbills/current
EnvironmentFile=/etc/govbills/govbills.env
ExecStart=/srv/govbills/venv/bin/gunicorn \
  config.wsgi:application --bind 127.0.0.1:8000 \
  --workers 2 --timeout 180 \
  --access-logfile - --error-logfile -
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
UMask=0027

[Install]
WantedBy=multi-user.target
```

Use a separate service with the same protected environment for:

```
/srv/govbills/venv/bin/python manage.py \
  run_scheduler --loop 300
```

The scheduler processes reminders/escalation and due-date work. Do not start multiple schedulers unintentionally; qualify task idempotency and monitoring. SMTP and SMS still need working transport configuration.

At the HTTPS proxy, preserve Host and set X-Forwarded-Proto from the actual trusted connection. Never trust a client-supplied forwarded-proto header directly. Restrict Gunicorn to loopback or a Unix socket. If the proxy is on another host, restrict forwarded-header trust to its address, not every source.

Set upload limits and timeouts consistently at proxy and application levels. Nginx may serve the collected public static directory; it must never alias PRIVATE_STORAGE_ROOT or SIGNING_CA_DIR. The application delivers private files only after authorisation checks.

The standalone URL configuration does not promise the Replit-only /healthz/ readiness endpoint. Configure an appropriate on-site readiness probe that checks application/database readiness without creating records, bypassing authentication or exposing sensitive diagnostics. A login-page 200 alone does not prove every dependency is healthy.

## 5. Code organisation and engineering practice

| Source package | Responsibility |
| --- | --- |
| config | Django settings, URLs, WSGI and deployment adapters |
| accounts | Login, second-factor flow, lockouts, password administration and profiles |
| org / providers | Ministry hierarchy, scoped roles, service accounts, provider data and billing cycles |
| imports / ocr | Folder/ZIP/spreadsheet intake, staged review, reusable provider mappings and OCR |
| invoices | Invoice identity, dates, amounts, duplicates, surcharge classification and evidence |
| workflow | Policies, approval tasks, segregation of duties, SLA timing and process visibility |
| operations | Batch preparation, ownership, membership history, disputes and adjustments |
| settlements | Grouped instructions, signatures, retained exports, invoice-level remittance references and execution records |
| reconciliation | Provider matching, exception routing and separate post-settlement bank checks |
| signatures | Signing identities, encrypted keys, PDF signatures and verification support |
| integrations | Administrative gateways, runtime settings, authorised mail intake, REST and read-only MCP |
| core / analytics | Shared audit, notifications, private storage, personal work and scoped reporting |
| templates / static / tests | Server-rendered UI, assets and regression coverage |

Business transitions belong in the existing service/engine layer, not ad-hoc model updates or SQL. Preserve transaction boundaries and repeat validation when a user acts. Use parameterised database access, Django CSRF forms, explicit scope checks and private download routes.

Treat approved invoice snapshots, signed PDFs, retained bank exports, batch membership and execution references as historical evidence. Post-payment disputes, waivers and refunds must be separate authorised records, not rewrites of the original signed settlement.

Development checks:

```
python -m pip install -r requirements-dev.txt
python manage.py check
python -m pytest
python manage.py verify_audit_chain
```

Run tests in an isolated test database and private test storage. Do not run seed_demo or seed_stamp_demo against a live database; those commands refuse production use. Do not copy passwords, certificates, signed URLs, source documents or production data into Git.

For this documentation release, Django system checks and the static-asset build passed. Thirteen focused walkthrough/reference-setup regression tests passed. The earlier dependency remediation recorded 535 passing regressions. A fresh full-suite run and an on-site installation acceptance run were not performed for this guide. Signed-in browser verification of the new walkthrough was not completed.

## 6. Security controls enforced in application code

The following is an implementation inventory, not an external assurance opinion. Enforcement depends on deployment settings, configured identities and correct operational use.

| Control | Enforcement in source | Important limit / evidence |
| --- | --- | --- |
| Password storage | Django authentication with Argon2-first hashing and password validators | Operators still control password policy and account lifecycle |
| Two-factor access | Session middleware requires second-factor verification before protected screens | Temporary fixed verification in Replit adapters must be removed before live use |
| Session protection | Idle/max-age controls, HTTP-only and SameSite cookies; secure __Host- cookies in production | Requires valid HTTPS proxy configuration |
| Brute-force resistance | Login and signing-PIN attempt tracking / lockout checks | Shared cache and client-IP trust require multi-worker qualification |
| CSRF and origin controls | Django CSRF forms, trusted origins, host checks and MCP Origin rejection | Do not use wildcard hosts/origins as an installation shortcut |
| Ministry / role scope | Server-side scoped queries and role checks on operational actions and downloads | Superuser access is exceptional and must be tightly governed |
| Segregation of duties | Invoice creator/submitter cannot approve the same invoice; independent payment authorisation | Do not bypass by directly changing database statuses |
| Duplicate bills | Provider invoice identity and account/period/currency/amount collision checks | File names alone are not invoice identity |
| Reconciliation gates | Provider evidence required before approval and settlement; mismatches remain unresolved | Auto-matching is not automatic approval or payment |
| Batch integrity | Ministry, provider and currency consistency; controlled membership and lifecycle history | Grouped settlements retain constituent batch identities |
| Financial evidence | Retained instruction/export content, signed PDFs, references and independent execution data | Bank file submission is not proof of bank execution |
| Sensitive fields | Fernet-encrypted sensitive database values and protected signing material | Encryption keys must be backed up and access-controlled independently |
| Private documents | Authorised file routes; private storage outside public static files | Filesystem/proxy permissions are an infrastructure responsibility |
| API tokens | Hash-only token storage, scopes, expiry, IP restrictions, rate checks and logging | Network exposure and token ownership still require administration |
| MCP restriction | Read-only tools; no approve, sign or pay capability | Do not add privileged tools without a separate threat review |
| Upload validation | Type/size checks; bounded ZIP intake; path traversal and symlink rejection | This is not proof of universal antivirus scanning |
| Incoming e-bills | Mailbox/sender/provider rules and trusted-relay clean-scan attestation checks | Mail retrieval, sender authenticity and malware scanning depend on the external relay |
| CSV safety | Formula-leading cells are neutralised in normal operational exports | Apply the same protection to any new exports |
| Audit integrity | Hash-chained events and verify_audit_chain | Tamper-evident, not immutable external notarisation or a substitute for restricted DB access |
| Security headers | Production HTTPS redirect/HSTS and additional application headers | Review CSP, proxy and HSTS consequences before enabling subdomain-wide policy |
| Configuration checks | Production refuses missing key secrets and DEBUG enabled | PostgreSQL, Redis, backups, gateway readiness and PKI accreditation are not all automatically guaranteed |

Use docs/SECURITY.md and its referenced code/tests as the detailed control map. A dependency audit is distinct from static analysis, penetration testing, operational review and legal/compliance approval.

## 7. Signing, file handling and integration boundaries

The internal signing CA and staff signing keys support operational PDF signing. A generated internal certificate is not automatically a nationally recognised or qualified electronic signature. Legal validity, trust roots, certificate policy and timestamping must be approved by the organisation.

Where configured, an RFC 3161 TSA can add signing timestamps. Server-side signing decrypts key material in process memory for the signing operation. HSM, PKCS#11, smart-card or remote-signing integration is a separate engineering/security scope, not an enforced feature of this baseline.

The bounded folder/ZIP intake limits the intake to 100 supported files and bounds expanded content to 100 MB, with additional per-file limits. Nested ZIPs, unsafe paths and encrypted/symlink entries are not treated as normal invoice files. Configure antivirus/quarantine before OCR as part of the on-site integration; do not mistake file-type checks for a malware engine.

For dedicated invoice mailboxes, map authorised senders to providers in Admin and use a trusted relay to authenticate mail and attest clean attachments. An allowlisted sender address by itself is not cryptographic proof of sender identity. Restrict relay credentials and ingress paths.

The final bank-specific file layout/transport remains an integration prerequisite. Existing retained exports and invoice-level entry references do not mean a bank has approved the format. Validate sample files, provider confirmations, duplicate release prevention and reconciliation against the actual bank before live operation.

## 8. Backup, recovery, monitoring and release control

Back up the PostgreSQL data, private source/generated files, encrypted signing material, all required field-encryption keys and approved runtime configuration as one recoverable system. Database-only backups are insufficient; losing encryption keys may make restored records unusable.

Use encrypted, access-controlled, off-host backups with retention approved by records/security owners. Set and test RPO/RTO objectives; this guide makes no recovery-time guarantee. PostgreSQL PITR and storage snapshots are infrastructure choices, not automatically configured by the application.

Restore in an isolated network first. Check migrations, private file access, encrypted-field decryption, certificate/signature verification, audit-chain verification, representative invoices and bank references. Disable outbound mail, SMS and bank routes during recovery drills.

Monitor application/worker uptime, database health, disk capacity, OCR latency, shared cache, scheduler heartbeat, gateway failures, authentication lockouts, failed PINs, changed provider bank details, overdue approvals and audit-chain failures. Ship protected logs to the organisation's SIEM without passwords, tokens or document content.

For each upgrade: back up, review migrations and dependencies, test in staging, run checks/tests, apply the approved on-site migration step, collect static files, restart services and verify staff journeys. A code rollback may not reverse a database migration; require a separate tested data-recovery plan for destructive changes.

## 9. Live-use acceptance checklist and outstanding actions

- Confirm exact server OS/CPU, supported Python/PostgreSQL/Redis versions and capacity test results.
- Prove PostgreSQL is selected; prevent accidental SQLite fallback.
- Remove fixed/shared verification and demo credentials; enrol individual authenticators.
- Establish named, scoped staff accounts and independent review/payment authority.
- Replace demo providers, accounts, tariffs and bank information with approved master data.
- Use HTTPS, explicit hosts/origins, trusted proxy headers and restricted admin/DB/cache networks.
- Store secrets and signing/field-encryption keys in approved restricted storage and test recovery.
- Configure and verify scheduler, private document delivery, native OCR and required language data.
- Configure malware scanning/quarantine and trusted mailbox relay before unattended intake.
- Obtain PKI/legal approval, bank-format acceptance and controlled notification transport.
- Re-run package auditing and security tests; commission independent review/penetration testing.
- Verify duplicate rejection, provider reconciliation gates, self-approval rejection and signed evidence.
- Demonstrate full settlement and bank matching using independent test actors in an isolated UAT environment.
- Keep walkthrough simulations separate from operational reporting and signed evidence.
- Complete backup/restore drill, monitoring, operating procedures and incident ownership sign-off.

The current walkthrough uses the existing administrator only. Its separate simulation catalogue shows completed financial scenarios without creating real signatures or transfers. Actual sample batches can remain draft or legitimately pending approval. This is intentional governance, not evidence that a live settlement has occurred.

No on-site installation, real bank transfer, legal certification, penetration test or disaster-recovery guarantee is claimed by this PDF.

## 10. Source references and handover

Primary source files reviewed: README.md, requirements.txt, requirements-dev.txt, config/settings.py, config/wsgi.py, config/replit_production.py, config/temporary_auth.py, docs/SECURITY.md, docs/dependency-security.md, docs/solution-design.md, core/files.py, imports/bulk.py, integrations/inbound.py and workflow/management/commands/run_scheduler.py.

Related implementation areas: accounts/security.py and middleware; org/access.py; workflow/engine.py; settlements/services.py and grouping/remittance modules; reconciliation services; signatures services; integrations gateway/MCP/configuration; core encrypted fields and audit model.

The GitHub source synchronisation preserves the standalone Django-root layout. Runtime databases, private uploads, keys, environment files, generated videos and this generated PDF are excluded from the source update. The editable guide and PDF-generation script are retained in source; download the PDF separately.

Release owners should record the approved commit, environment configuration, database migration review, validation evidence, infrastructure ownership and final go-live sign-off in their own change-management system.
