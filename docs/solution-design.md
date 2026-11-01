# GovBills
## Solution Design Document

As-built application and controlled integration roadmap

Prepared 29 October 2026. Audience: executive sponsors, ministry operations, finance, system administrators, security and implementation teams.

**Document boundary.** This document describes the current application design and distinguishes implemented controls from integrations, configuration and operational assurance still required. Current-interface illustrations use development/demo records, not evidence of a live bank transfer. Intended benefits are not measured savings or a compliance certification.

## 1. Executive summary and scope

GovBills manages utility invoice receipt, account registration, OCR-assisted checking, provider reconciliation, batch approval, settlement preparation and post-settlement bank reconciliation. Ministry ownership and assigned roles determine what a user can see and act on. Each processing batch is limited to one utility provider. Approved batches for that provider can be grouped for settlement without losing their original identities or audit histories.

The operating objective is less repeated entry, earlier intervention on overdue work and a traceable handoff between records staff, approvers and finance. Leadership can drill from scoped dashboards into the underlying records. Operational results must be baselined during a controlled pilot; no processing-time reduction or surcharge saving is assumed.

| Capability | Design position |
| --- | --- |
| Folder/ZIP intake, staged review and account registration | Implemented in the application; source documents and review remain part of the journey. |
| Provider OCR templates and per-invoice checking | Implemented; extraction is assistance, not approval. Representative layouts and scan quality require pilot validation. |
| Batch approvals, provider reconciliation and settlement grouping | Implemented controls; role and ministry boundaries must be validated for real staff. |
| Individual invoice settlement entries | Implemented reference CSV/XML output and stable entry identifiers; bank-specific layout is pending. |
| Provider settlement advice | Exact retained CSV copy and externally recorded delivery evidence; not an automatic email service. |
| Dedicated e-bill mailbox | Administrative mappings and secure ingestion endpoint are implemented. A real mailbox/relay connection is still required. |
| Notifications | In-application notifications and administrative gateway configuration exist; live email/SMS depend on configured transports. |
| Live operation | Requires verified onboarding, individual authentication hardening, integration acceptance and operational sign-off. |

## 2. Operating model and responsibilities

The organisational hierarchy represents government, ministries and subordinate units/sites. Access is enforced by application-level organisational scope and role checks. This is not a claim of database row-level security. Ministry administrators manage their own operations; central administrators have explicitly broader responsibilities.

| Responsibility | Primary role | Control or outcome |
| --- | --- | --- |
| Capture and verify receipt | Clerk / records processor | Upload records, inspect detected invoices and check dates and amounts. |
| Register service accounts | Unit administrator | Resolve missing accounts, service purpose, owner and status. |
| Prepare batches | Assigned processing staff | Select a single provider, confirm newly processed invoices and preserve ownership history. |
| Review and approve | Assigned workflow reviewers and approvers | Batch-based approval basket and traceable task assignments. |
| Prepare settlement | Finance | Reconcile approved amounts, group eligible batches and prepare instructions. |
| Authorise settlement | Independent payment authoriser | Separate from the preparer; recorded signing evidence. |
| Record execution and bank match | Authorised finance / reconciliation staff | Keep bank submission, payment execution and statement matching distinct. |
| Oversight | Ministry leadership / auditor | Permission-scoped dashboards and inspectable evidence. |
| Configure connections and policies | System administrator | Workflow, gateway, sender mappings and access configuration. |

My Space links existing GovBills folders and exposes providers, batches, invoices, services and assigned tasks. Pinned folders provide navigation shortcuts; they do not create new permissions. My Work separates assigned operational tasks, personal processing schedules and anomalies submitted by the user from financial liability reporting.

## 3. End-to-end user journey

**Receive → read → check → reconcile → batch → approve → group → authorise → submit → advise provider → execute → bank-match.**

1. Receive a file, folder or ZIP, or accept an authenticated e-bill through the configured relay. Preserve the source document and actual receipt information.
2. Detect invoice details with OCR where needed. Reuse a provider layout already mapped; show extracted values for verification rather than requesting a fresh mapping for every invoice.
3. Resolve missing service accounts through the registration workflow. The batch wizard offers detected invoices and additional uploads; confirmed additions update the ministry and preparer ownership as required.
4. Check receipt-date anomalies, due-date urgency, amounts, currency and surcharge lines. Exceptions remain visible rather than silently treated as valid.
5. Import the provider pending-invoice listing and reconcile account, invoice reference and amount. Mismatches enter the Unsorted basket for ownership and resolution.
6. Prepare single-provider processing batches. Route approved/rejected work through the assigned batch workflow, with a collapsed journey map and visible current stage.
7. Group eligible approved batches for one provider and currency within the allowed ministry scope. Preserve batch membership and amount snapshots.
8. Finance signs preparation evidence; an independent authoriser countersigns. Revalidate reconciliation before bank release.
9. Prepare individual invoice entries and retain the reference attachment. Record external bank submission only when it occurred. The bank-ready adapter awaits the bank specification.
10. Supply the respective provider with the same final bank attachment once that interface is implemented. Current functionality retains an exact CSV reference copy and records delivery made outside GovBills.
11. Record actual execution with the bank reference and value date. Only execution changes invoice payment state.
12. Import and match bank statement evidence. Investigate discrepancies without treating case ownership or manual closure as proof of financial reconciliation.

Rejected work, disputed invoices and missing prerequisites remain separate operational queues; they do not bypass the financial gates.

## 4. Logical and deployment architecture

The staff portal is a Django application with server-rendered pages. Its domain modules cover accounts/organisation, providers and service accounts, intake, OCR, invoices, workflows, operational batches, settlements, signatures, reconciliation, reporting and integrations.

Production uses Gunicorn and Replit hosting with managed PostgreSQL for transactional data. Invoice sources, retained settlement exports and signing-related private files use the configured private App Storage adapter. Documents are served through permission-checked application routes, not public storage URLs.

The development preview retains a separate SQLite workspace dataset. Managed development PostgreSQL supplies the schema that Publishing can synchronise to production. This does not imply that preview demo records are copied to production.

| Component | Responsibility | Boundary |
| --- | --- | --- |
| Staff browser | Forms, dashboards, filters, notifications and evidence access | HTTPS session and organisational permissions. |
| Django / Gunicorn | Business rules, state transitions, signing orchestration and downloads | No automatic production schema mutations at startup. |
| PostgreSQL | Accounts, tasks, invoices, batch links, payment evidence and audit metadata | Replit-managed production database; controlled Publish schema flow. |
| Private App Storage | Source files, PDFs and immutable retained exports | Application-authorised retrieval and integrity checks. |
| OCR runtime | PDF/image text extraction and provider layout reuse | Extracted values require verification; no approval authority. |
| Trusted mailbox relay | Mailbox retrieval, sender authentication and clean-scan attestation | External connection/configuration required. |
| Bank / treasury interface | Final payment-file submission and execution evidence | Final layout and transport not yet supplied. |
| Email/SMS transport | Configured outbound notifications | Do not infer live delivery from saved gateway settings. |

The repository also contains a companion API service and a presentation artifact. They are not a substitute for the Django financial transaction controls.

## 5. Core information model

| Aggregate | Principal relationships and evidence |
| --- | --- |
| Organisation and membership | A user holds roles within an organisational scope; assignment and scope determine actionable work. |
| Provider and service account | Provider → account → owning unit; account service type, purpose, owner and lifecycle status. |
| Intake and source document | Upload/import or accepted mailbox delivery → staged record → invoice; original source retained. |
| OCR template | Provider/layout mapping reused for later invoices; extracted field checks remain invoice-specific. |
| Invoice | Internal reference, provider invoice number, account, dates, currency, amounts, surcharge and workflow evidence. |
| Processing batch | Batch members preserve invoice membership, preparer/ministry ownership and lifecycle events. |
| Settlement instruction/group | A settlement instruction contains provider totals and invoice items; group links retain member batch snapshots. |
| Invoice settlement entry | Each settlement item has its own deterministic operational entry reference and captured payable amount. |
| Retained export | Instruction + format version → private file, checksum, entry count, creation actor and timestamp. |
| Signature evidence | Signing identity, signed PDF hash and preparation/authorisation events. |
| Bank reconciliation | Imported statement lines, settlement links and discrepancies; matching remains distinct from payment execution. |
| Dispute and adjustment | Invoice/item issue, ownership, evidence and authorised outcomes; post-payment records do not rewrite original payments. |
| Audit and notification | Append-only hash-chained events and scoped action notifications. This is tamper evidence, not a claim of external notarisation. |

The invoice entry reference is an internal handoff identifier, not a bank-issued settlement reference. The execution reference is recorded separately when the bank/treasury confirms execution.

## 6. Intake, OCR and service-account controls

Folder/ZIP intake stages invoices before committing them to the financial journey. The wizard supports detected invoices and additional files, and asks the user to confirm processing newly added records. First-time accounts route to registration rather than being silently created with guessed ownership.

Service accounts include status (active, on hold, decommissioned, dispute or suspended), service details/type, purpose and owner. Account register filters cover provider, ministry, service, lifecycle and invoicing status. Dashboard figures and drill-through listings should share the same filter scope.

OCR reads supported PDFs and images using the application's configured extraction/rasterisation pipeline. A known provider template is reused; staff inspect extracted fields against the source. Low-confidence values, missing account matches, duplicate candidates, unusual receipt dates and urgent due dates need explicit review. Pilot acceptance must include representative provider layouts, rotated scans, poor-quality images and multi-page documents.

The dedicated mailbox design maps authorised sender addresses to utility providers under administration. A single-purpose ingestion client is bound to a processing identity and organisational scope. The relay must verify aligned sender authenticity, scan attachments and submit integrity-bound original mail and trusted receipt metadata. Sender-controlled headers alone are not accepted as proof.

The endpoint rejects unauthorised/unmapped senders, holds missing clean-scan evidence before attachment parsing and recognises exact delivery replays without duplicate staging. Successful ingestion stages invoices only: it never approves or pays them. Saving a mailbox address does not connect Outlook.

## 7. Workflow, approval and financial controls

Provider reconciliation is mandatory before approval and again before settlement. Provider-list imports are compared with account, invoice reference and payable amount. Unsorted exceptions require resolution and reassignment; an ownership change is not reconciliation.

Approval work is batch-based. Role assignments and workflow configuration determine the responsible reviewer/approver and the current journey stage. Batch preparation, Work in Progress and Rejected folders are navigation/work organisation, not independent financial states.

Settlement grouping preserves each approved batch instead of merging away its identity. Membership, totals and audit links remain inspectable after cancellation or execution. Currency and ministry boundaries remain in force.

The preparer cannot authorise their own instruction. Signing PIN and identity checks protect the signing actions; signed PDFs and fingerprints preserve evidence. The application signing implementation is not a claim of government-accredited PKI: certificate trust, identity proofing and policy acceptance need formal approval.

Disputes may be raised for invoices/items, including surcharge issues and post-payment cases. Approved pre-payment adjustments can change payable amounts under authorised controls. Post-payment waivers/refunds remain separate records and must not rewrite signed historical snapshots or the original settlement.

Operational invoice stamps show recorded review/approval identities, dates, batch and available settlement details, including recorded signature images. They are derived operational copies, not replacements for source invoices or a claim that the stamped copy itself is digitally signed. Missing evidence must be labelled.

## 8. Bank file and provider-confirmation design

**Required outcome:** the bank receives individual invoice entries and references. The same final bank file is then supplied to the respective utility provider as settlement confirmation.

**Current implementation:** the reference CSV contains one row per invoice, including settlement/group and batch references, entry reference, provider invoice number, internal invoice reference, service account, due date, currency, payable amount, beneficiary details and requested execution date. The XML reference example also represents invoice-level transactions. Neither is certified against the bank's future specification.

The CSV is retained as a private, versioned export. Repeated downloads and the provider copy use the same retained bytes and checksum, rather than regenerating them from subsequently changed display data. External bank submission records the reference schedule checksum. Provider delivery records the attachment checksum, provider, recipient, delivery receipt, actor and time. Duplicate recording of the same receipt is idempotent.

Provider advice before execution means **submitted to bank, not paid**. Current provider delivery is an operator attestation of an external email/portal handoff; GovBills does not claim to have sent that email. Actual payment execution and bank reconciliation remain independent.

| Interface decision | Required before enabling bank-ready output |
| --- | --- |
| File specification | Sample, schema/layout, encoding, delimiters or fixed widths, field lengths and versioning. |
| Entry contract | Mandatory invoice/account/beneficiary references, uniqueness rules and rejection codes. |
| Amounts | Currency precision, rounding policy, control totals, charges and partial-payment rules. |
| Bank booking | Confirm whether one batch debit or individual invoice debits appear on statements; extend matching if required. |
| Transport | Approved secure bank channel, acknowledgement/retry rules and submission authority. |
| Provider delivery | Approved recipient/channel, identical-attachment policy, delivery receipt and retry controls. |
| Acceptance | Bank sample validation, rejection tests and a controlled non-live trial before release. |

Future bank adapters should produce a new retained format version. They must not overwrite earlier reference exports, original documents or signed evidence. Automated provider delivery must not be marked successful without a confirmed transport outcome.

## 9. Visibility, reporting and operational management

Key work areas expose Overview dashboards and drill-through registers. Provider selection defaults to all providers; filters must carry into matching listings. My Work shows assigned tasks, processing schedules, submitted anomalies and overdue work. My Space offers personalised access to existing folders and current operational records.

The left navigation remains persistent. A compact notification icon/dropdown surfaces overdue work and action alerts without occupying a large page banner. Overdue counts represent work items: one invoice can contribute to more than one operational category, so they must not be presented as unique invoice counts.

Historical dashboards analyse provider/ministry trends within the user's authorised scope and without mixing currencies. Ministry timeliness reporting connects receipt/submission/due-date observations with billed surcharge amounts and drill-through evidence. A correlation between delay and a surcharge does not by itself establish fault or causation.

Recommended pilot measures are elapsed processing time, OCR correction/review rates, unassigned exceptions, reconciliation mismatches, approval ageing, overdue items, provider-advice timeliness and completeness of settlement evidence. These are measures to baseline, not achieved performance claims.

Retention periods, statutory reporting, approved surcharge treatment and the definition of late submission require policy-owner sign-off. The solution is not represented as legally certified or automatically compliant.

## 10. Security and audit design

Authentication uses application accounts, passwords and a second-factor workflow. A temporary fixed verification configuration exists by explicit development decision and must be replaced with individual two-factor setup before unrestricted live use. This document intentionally contains no passwords, signing PINs, setup-token values or production credentials.

The sign-in identifier is the stored username. An email address is not interchangeable unless it is itself the username. Production accounts use their own configured passwords; preview demo credentials are not automatically production credentials.

Initial production administration is a one-time flow protected by a separate strong setup token and attempt throttling. It is disabled once an administrator/bootstrap record exists. A rejected setup token is not evidence of storage failure. Subsequent password administration must preserve access boundaries and audit accountability.

Secrets are supplied through environment/secret configuration. Permanent encryption/signing secrets must not be casually rotated: existing encrypted records or signing material may depend on them. Document downloads require permission checks, and export text is neutralised against spreadsheet formula injection.

Signed evidence, original documents, payment releases and post-payment outcomes remain distinguishable. Retained settlement files are hash-checked before use. Auditors inspect the application trail, but backup/restore controls and access monitoring are also required; a hash chain alone does not supply disaster recovery or prevent every privileged database action.

Open assurance items include production identity hardening, end-to-end access testing, gateway credential lifecycle, malware-scan operation, log redaction, evidence retention and recovery exercises.

## 11. Deployment, support and recovery

Production runs the application against managed PostgreSQL and private App Storage. Development schema preparation is restricted to development mode and must reject Replit's published-runtime indicator. Publishing, not a custom startup command, provisions/synchronises the production schema.

Apply and verify migrations against managed development PostgreSQL before publishing. Do not add production DDL or demo seeding to build/start commands. Keep the preview SQLite dataset separate; only transfer records with explicit approval.

Release checks should include build/static assets, schema compatibility, authenticated role journeys, document retrieval, signing/evidence checks and health/readiness. Startup success does not prove a healthy database or a complete production schema. Current readiness primarily checks tables; safe diagnostics and required-column coverage need further hardening.

Define backup ownership, schedules, recovery-point/recovery-time objectives, retention, data residency and incident escalation before operational acceptance. Values are to be agreed, not assumed. Exercise a restore of both transactional records and associated private files without rotating permanent encryption material.

Rollback should consider schema compatibility, signed evidence and data preservation, not only code. Bank execution and external provider delivery cannot be undone by reverting application code.

## 12. Verification and rollout decisions

Recent targeted verification covered 62 settlement/remittance, grouping, bank-reconciliation and import tests. The checks include individual entries, reference uniqueness, exact retained file copies, financial totals in the tested cases, permission boundaries, external-delivery recording, duplicate receipts, integrity failures and preservation of invoice payment state. This is scoped regression evidence, not a full security audit, full production acceptance or proof of a live bank transfer.

The presentation screenshots accompanying this document must come from the current running development UI. They illustrate real application screens with demo data; they do not establish live mailbox connectivity or bank execution. Capture dates, page paths and any privacy redactions are recorded with the image set.

| Gate | Acceptance evidence / owner |
| --- | --- |
| Ministry operating model | Approved user/role matrix and representative access tests; ministry owner. |
| Intake and OCR | Provider layout samples, poor-scan cases and exception review; records lead. |
| Approval and disputes | Assigned batch workflow, independent authority and adjustment policy; finance/policy owner. |
| Bank and provider interfaces | Approved bank layout, booking semantics, transport and delivery trial; bank liaison and finance. |
| Security | Individual authentication, scoped gateways, secret handling and private-file access; security owner. |
| Operations | Tested restore, monitoring and support process; platform/operations owner. |
| Go-live | Controlled pilot results and signed acceptance; executive sponsor. |

Priority decisions are the bank file specification, debit-booking granularity, real mailbox relay, outgoing delivery channels, legal retention/data residency and the end of temporary authentication settings. Expansion should follow measured pilot results and explicit owner sign-off.

## Appendix A. Design evidence and source map

The as-built description was checked against the application configuration, domain models/services, current interface capture pass and recent regression results. The following source locations support implementation review; they are not end-user URLs.

| Area | Source location |
| --- | --- |
| Production / schema configuration | `config/replit_production.py`, `config/replit_schema.py`, `config/runtime_database.py` |
| Intake and mailbox ingestion | `imports/`, `integrations/inbound.py`, `integrations/inbound_models.py` |
| Organisational scope | `org/access.py`, `org/models.py` |
| Invoice, OCR and batch journey | `invoices/`, `ocr/`, `operations/` |
| Settlement grouping and references | `settlements/grouping.py`, `settlements/services.py`, `settlements/remittance.py` |
| Retained attachments | `settlements/models.py`, `core/app_storage.py` |
| Reconciliation / bank evidence | `reconciliation/services.py`, `reconciliation/bank.py` |
| Audit / signing / reporting | `core/models.py`, `signatures/`, `analytics/` |
| Scoped verification | `tests/test_settlement_remittance.py`, `tests/test_settlement_groups.py`, `tests/test_bank_reconciliation.py`, `tests/test_settlement_imports.py` |

## Appendix B. Current interface evidence

The PDF edition includes selected current captures and their source page paths. Screenshots demonstrate the present interface only; any masked sensitive content is noted. No previous-interface screenshots are used as substitutes.
