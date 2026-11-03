# Dependency security maintenance

## Remediation verification — 3 November 2026

The workspace dependency audit initially reported 178 findings:
6 critical, 48 high, 82 moderate and 42 low. These include findings repeated
across the runtime and development requirements files, not 178 separate packages.
The audit after remediation reported **zero findings at every severity**.

### Python runtime and standalone installation

The workspace already had patched packages installed. Explicit requirements now
keep standalone installations aligned with the workspace lockfile:

| Package | Required version |
| --- | --- |
| aiohttp | 3.14.5 |
| certifi | 2026.10.23 |
| idna | 3.20 |

Both the root Python project and Django requirements declare these versions.
Development requirements inherit the runtime requirements. The existing Python
environment passes `uv pip check --python .pythonlibs/bin/python`, and
`uv lock --check` confirms the lockfile matches the project requirements.

### JavaScript dependencies

The workspace lockfile resolves the affected packages to patched releases:

| Package | Resolved release |
| --- | --- |
| body-parser | 2.4.0 |
| qs | 6.17.1 |
| fast-uri | 3.1.9 |
| postcss-selector-parser | 6.1.5 |
| prosemirror-view | 1.42.9 |
| undici | 7.30.1 and 8.12.3 |
| zod | 4.6.7 |

The OpenAPI generator was updated to obtain patched transitive dependencies.
The application schemas type-check with Zod 4. The presentation editor's shared
ProseMirror model dependency was aligned and deduplicated; editor source and
document behavior were not rewritten.

The latest typography package still pins an older selector parser, so the
workspace retains a targeted same-major override. The existing same-major
fast-uri override was raised to its patched release.

### Verification and publishing

- Dependency audit: zero findings, without ignoring advisories.
- Full workspace TypeScript checks and Django system checks pass.
- All 535 Django regression tests pass.
- Portal static-asset, API and presentation production builds pass.
- Portal login, API health and presentation routes respond successfully.
- Package minimum-release-age protection and package firewall remain enabled.
- No authentication controls, financial records, database schema or production
  data were changed by this dependency update.

The updates take effect in production only after the user publishes the current
workspace. A passing dependency audit addresses known package advisories; it is
not a claim that all application-security risks have been eliminated.
