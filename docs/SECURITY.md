# Security design and controls

This document lists the controls built into GovBills, where each one is implemented and how it is tested, so that a
security reviewer can check every claim.

## Threats addressed

| Threat | Control | Where | Test |
|---|---|---|---|
| Stolen password | TOTP two-factor login is mandatory for everyone; until the second factor passes, only the 2FA pages are reachable | `accounts/middleware.py`, `accounts/views.py` | `test_password_alone_does_not_grant_access` |
| Captured TOTP code replayed | The last accepted time-step is stored; a code is never accepted twice | `accounts/security.py::verify_totp` | `test_totp_login_succeeds_and_code_cannot_be_replayed` |
| Password guessing | Lockout after `LOGIN_MAX_FAILURES` failures per user, plus per-IP throttling; error messages don't reveal which part failed | `accounts/security.py` | `test_lockout_after_repeated_failures` |
| Lost phone | 10 single-use recovery codes, stored as hashes | `accounts/models.py::RecoveryCode` | `test_recovery_code_is_single_use` |
| Unattended session | 15-minute idle timeout, 8-hour maximum session, logout on browser close, session ID rotated after 2FA | `accounts/middleware.py`, settings | `test_idle_session_expires` |
| Weak or temporary passwords | Argon2 hashing; minimum 12 characters with mixed character types; must differ from the previous password; temporary passwords must be changed at first sign-in | settings, `accounts/validators.py` | `test_forced_password_change` |
| Cross-unit data access | Every query is scoped to the units below the user's role grants; objects outside scope return 404 | `org/access.py` | `test_out_of_scope_invoice_is_hidden`, `test_roles_apply_to_subtree_only` |
| Collusion or self-approval | Recorder and submitter can't approve; one person can decide only one step per round; settlement preparer ≠ authoriser | `workflow/engine.py`, `settlements/services.py` | `test_creator_cannot_approve_even_with_role`, `test_high_value_needs_extra_step_by_different_person`, `test_settlement_end_to_end` |
| Payment diversion (fake bank-change letter) | Bank details change only with two administrators; the request records how the change was verified; IBAN checksum enforced; the admin site cannot edit bank fields | `providers/views.py`, `providers/admin.py` | `test_bank_details_need_second_admin`, `test_invalid_iban_rejected` |
| Invoice paid twice | Unique (provider, invoice number), checks for duplicates within an import file and for reused documents, plus possible-duplicate flags | `invoices/models.py`, `invoices/services.py`, `imports/services.py` | `test_duplicate_invoice_rejected`, `test_csv_import_stages_validates_and_commits` |
| Figures changed after approval | Approval signatures cover the invoice snapshot and document hash, and verification compares them with the current record | `signatures/services.py::verify_signature` | `test_signature_detects_later_change_to_invoice` |
| Payment instruction altered | PAdES signatures; the stored PDF's hash is checked before counter-signing; a verification page is provided | `settlements/services.py`, `signatures/services.py::validate_pdf` | `test_tampered_pdf_fails_validation`, `test_stored_pdf_hash_checked_before_countersigning` |
| Stolen signing key | Key is PKCS#8-encrypted with the user's PIN *and* Fernet-encrypted with the server key; PIN lockout after `SIGNING_PIN_MAX_FAILURES`; PIN failures are committed outside the business transaction | `signatures/services.py` | `test_wrong_pin_rejected_and_locks` |
| Covering tracks | Append-only audit log; each entry hashes the previous one; model save/delete are blocked; `verify_audit_chain` detects edits and deletions | `core/models.py::AuditEvent` | `test_audit_chain_detects_tampering`, `test_audit_chain_detects_deletion` |
| Malicious uploads | File type is detected by content (magic bytes), workbooks are checked for zip bombs, size is limited, file names are randomised, files are stored outside the web root, and downloads use `nosniff` and a `sandbox` CSP | `core/files.py`, `core/utils.py::file_response` | `test_upload_validation_uses_content_not_name`, `test_zip_renamed_as_xlsx_is_rejected` |
| Cross-site scripting | Django auto-escaping; strict CSP (`script-src 'self'`, no inline scripts or styles, `object-src 'none'`, `base-uri 'none'`) | `core/middleware.py` | `test_security_headers` |
| CSRF / clickjacking | Django CSRF with SameSite=Strict cookies; state changes require POST; `X-Frame-Options: DENY`, `frame-ancestors 'none'` | settings | `test_security_headers` |
| CSV / Excel formula injection | Exported cells starting with `= + - @` are prefixed with a quote | `core/utils.py::_safe_cell` | `test_csv_export_neutralises_formulas` |
| Database or backup leak | IBANs, TOTP secrets and signing keys are encrypted with rotatable Fernet keys (`FIELD_ENCRYPTION_KEYS`) | `core/fields.py` | `test_encrypted_fields_are_not_stored_in_clear` |
| Misconfiguration | Production mode refuses to start without its secrets or with DEBUG on, and turns on HSTS, SSL redirect, secure `__Host-` cookies and hashed static files | `config/settings.py` | `manage.py check --deploy` |

## Residual risks and recommendations

1. **Internal CA key on disk.** The CA key is encrypted with `SIGNING_CA_PASSPHRASE` and stored with 0600
   permissions. For production, prefer the national PKI (PKCS#12 import) or keep the CA key in an HSM. pyHanko
   supports PKCS#11 if you extend `signatures/services.py`.
2. **Server-side signing.** User keys are decrypted in server memory for the duration of each signing operation.
   For qualified signatures, integrate a remote signing service or smart cards / USB tokens instead.
3. **TLS termination and `REMOTE_ADDR`.** Configure the reverse proxy so the client IP reaches gunicorn
   (`--forwarded-allow-ips` set to the proxy address only, not `*`, when it is on another host).
4. **Admin URL.** Change `ADMIN_URL_PATH` and restrict it at the proxy to the ICT network.
5. **Monitoring.** Ship logs to the SIEM. Alert on `auth.login_locked`, `signature.pin_failed`,
   `provider.bank_change_*`, `approval.sla_breached` and any `verify_audit_chain` failure.
6. **Independent testing.** Commission a penetration test and a code review before production use.
