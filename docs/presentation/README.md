# GovBills — Receipt to Settlement

Authoring-source snapshot of the current 14-slide presentation: receipt, invoice checks and registration, provider reconciliation, batch preparation and approval, grouped settlement, independent authorisation, bank matching and operational benefits.

## Contents

- `src/pages/slides/`: the fourteen slide components.
- `src/data/slides-manifest.json`: slide order, titles and descriptions.
- `src/index.css`: shared presentation theme and layouts.

The canonical editable deck is the GovBills solution presentation in the Replit workspace (`artifacts/govbills-solution`). This documentation folder preserves its authoring source without replacing the repository's standalone Django layout. It is not an independently runnable application: the presentation runtime, demo screenshot assets and fonts remain in the Replit artifact. Relative source paths are retained so the manifest still identifies the corresponding components.

## PowerPoint download

The current PowerPoint is delivered separately through the Replit chat and Library as **GovBills — Receipt to Settlement (PowerPoint)**. Generated PowerPoint/PDF binaries are intentionally excluded from Git history.

## Demonstration scope

The journey reflects the current operational controls. Retained screenshots use demo records and show earlier interface styling, as labelled in the deck. No live bank transfer is demonstrated. Incoming e-bill processing requires a configured trusted mail relay; Outlook is not connected by simply saving a mailbox address. Temporary shared verification settings and notification gateways require production hardening/configuration.

## Updating this snapshot

Edit and verify the canonical Replit presentation first, then sync its slide components, manifest and theme into this folder. Preserve the Django repository root and unrelated files. Never include credentials, runtime databases, invoice uploads or generated downloads.
