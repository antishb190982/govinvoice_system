"""Fictional, read-only examples. Never write to financial or identity tables.

These are presenter illustrations, not imported invoices, authorised payments,
bank evidence or signed approvals. Rates are indicative, not official tariffs.
"""
from datetime import date, timedelta
from decimal import Decimal


AS_OF = date(2026, 11, 3)
MONEY = Decimal("0.01")
PROVIDERS = {
    "CEB": "Central Electricity Board",
    "CWA": "Central Water Authority",
    "MT": "Mauritius Telecom",
    "EMTEL": "Emtel Ltd",
    "WMA": "Wastewater Management Authority",
}
MINISTRIES = {
    "MOHW": "Ministry of Health and Wellness",
    "MOE": "Ministry of Education",
    "PMO": "Prime Minister's Office",
}
STAGES = {
    "preparation": "In preparation",
    "provider_check": "Provider reconciliation exception",
    "approval_pending": "Approval pending",
    "returned": "Returned for correction",
    "settlement_ready": "Ready for finance",
    "authorisation_pending": "Payment authorisation pending",
    "sent_to_bank": "Awaiting bank confirmation",
    "completed": "Settlement completed · bank matched",
    "bank_mismatch": "Bank amount discrepancy",
    "bank_unmatched": "Unmatched bank debit",
    "disputed": "Surcharge dispute / waiver pending",
}
PAID_STAGES = {"completed", "bank_mismatch"}
SETTLEMENT_STAGES = PAID_STAGES | {
    "settlement_ready", "authorisation_pending", "sent_to_bank",
}
SPECS = [
    ("CEB", "MOHW", "completed", 4, "Hospital electricity · settled group, first batch"),
    ("CWA", "MOE", "approval_pending", 3, "School water accounts · awaiting approval"),
    ("MT", "PMO", "settlement_ready", 4, "Government telephony · finance preparation"),
    ("EMTEL", "MOHW", "sent_to_bank", 3, "Clinic connectivity · awaiting bank confirmation"),
    ("WMA", "MOE", "bank_mismatch", 2, "School wastewater · bank debit differs"),
    ("CEB", "PMO", "provider_check", 3, "Government buildings · provider listing mismatch"),
    ("CWA", "MOHW", "preparation", 4, "Health-centre water · newly prepared"),
    ("MT", "MOE", "returned", 3, "School telephony · returned for correction"),
    ("CEB", "MOHW", "completed", 2, "Hospital electricity · settled group, second batch"),
    ("EMTEL", "PMO", "bank_unmatched", 2, "Government connectivity · unmatched debit investigation"),
    ("CEB", "MOE", "authorisation_pending", 4, "School electricity · independent authorisation pending"),
    ("CWA", "PMO", "disputed", 2, "Government water · late-charge waiver requested"),
]


def line(description, quantity, unit_price):
    quantity, unit_price = Decimal(str(quantity)), Decimal(str(unit_price))
    return {
        "description": description, "quantity": quantity, "unit_price": unit_price,
        "amount": (quantity * unit_price).quantize(MONEY),
    }


def bill_lines(provider, quantity, index=0):
    """Indicative line items, not provider tariffs or a bank file specification."""
    if provider == "CEB":
        return [line("Electricity consumption — indicative rate per kWh", quantity, "8.60"),
                line("Service / meter charge — illustrative", 1, "175.00")]
    if provider == "CWA":
        return [line("Water consumption — indicative rate per m³", quantity, "32.00"),
                line("Meter service charge — illustrative", 1, "95.00")]
    if provider == "MT":
        return [line("Government voice/data subscription — illustrative", 1, 1850 + index * 150),
                line("Usage outside bundle — illustrative", quantity, "0.75")]
    if provider == "EMTEL":
        return [line("Business connectivity subscription — illustrative", 1, 4900 + index * 250),
                line("Managed service option — illustrative", 1, "450.00")]
    return [line("Wastewater service units — indicative", quantity, "75.00"),
            line("Service charge — illustrative", 1, "125.00")]


def invoice_example(provider, ministry, number, index, *, settled=False, surcharge=False):
    start = date(2026, 8 if settled else 10, 1)
    end = date(2026, 8 if settled else 10, 31)
    issue = end + timedelta(days=1)
    quantity = {
        "CEB": 12600 + index * 1725, "CWA": 360 + index * 42,
        "MT": 850 + index * 125, "EMTEL": 1, "WMA": 42 + index * 8,
    }[provider]
    lines = bill_lines(provider, quantity, index)
    net = sum((row["amount"] for row in lines), Decimal("0"))
    tax = (net * Decimal("0.15")).quantize(MONEY) if provider in {"MT", "EMTEL"} else Decimal("0.00")
    penalty = Decimal("275.00") if surcharge else Decimal("0.00")
    previous = 38000 + index * 9100
    return {
        "number": number, "account": f"SIM-{provider}-{ministry}-{index + 1:03d}",
        "site": f"{ministry} demonstration premises {index + 1}",
        "service": {"CEB": "Electricity", "CWA": "Water", "MT": "Telephony",
                    "EMTEL": "Connectivity", "WMA": "Wastewater"}[provider],
        "owner": f"{MINISTRIES[ministry]} · illustrative billing team",
        "meter": f"SIM-METER-{provider}-{index + 1:03d}" if provider in {"CEB", "CWA"} else "Not metered",
        "period_start": start, "period_end": end, "issue_date": issue,
        "received_date": issue + timedelta(days=1), "due_date": issue + timedelta(days=25),
        "previous_reading": previous if provider in {"CEB", "CWA"} else None,
        "current_reading": previous + quantity if provider in {"CEB", "CWA"} else None,
        "quantity": quantity,
        "unit": {"CEB": "kWh", "CWA": "m³", "MT": "minutes", "EMTEL": "subscription",
                 "WMA": "service units"}[provider],
        "lines": lines, "net": net, "tax": tax, "surcharge": penalty,
        "total": net + tax + penalty, "currency": "MUR",
    }


def journey_for(scenario):
    phase = scenario["stage"]
    current = {
        "preparation": 0, "provider_check": 1, "approval_pending": 2, "returned": 2,
        "settlement_ready": 3, "authorisation_pending": 4, "sent_to_bank": 5,
        "completed": 7, "bank_mismatch": 6, "bank_unmatched": 6, "disputed": 1,
    }[phase]
    labels = [
        ("Receive and prepare", "Invoice details captured once; original evidence retained.", "Invoice preparer"),
        ("Reconcile provider listing", "Validate account, invoice number, period and amount.", "Reconciliation officer"),
        ("Review and approve", "Independent review and approval remain mandatory.", "Independent reviewer / approver"),
        ("Prepare settlement", "Retain provider-specific batches and invoice-level references.", "Finance preparer"),
        ("Authorise payment", "A different authorised staff member releases the instruction.", "Payment authoriser"),
        ("Record bank execution", "Bank execution reference is distinct from a prepared instruction.", "Finance officer"),
        ("Reconcile bank debit", "Match the statement and investigate discrepancies.", "Bank reconciliation officer"),
    ]
    result = []
    for index, (label, detail, role) in enumerate(labels):
        state = "done" if index < current else "current" if index == current else "pending"
        if index == current and phase in {"provider_check", "returned", "bank_mismatch", "bank_unmatched", "disputed"}:
            state = "attention"
        if index == current:
            detail = scenario["attention"] or scenario["next_action"]
        # Unmatched money is not proof of any invoice approval or execution.
        if phase == "bank_unmatched" and index < 6:
            state, detail = "pending", "Not evidenced: do not infer approval or settlement from an unmatched debit."
        when = min(AS_OF, scenario["invoices"][0]["issue_date"] + timedelta(days=index + 1)) if state == "done" else None
        result.append({"label": label, "detail": detail, "state": state, "when": when, "role": role})
    return result


def scenarios():
    """Return fresh dictionaries so filter/export decoration cannot leak across requests."""
    result = []
    attention = {
        "provider_check": "Provider pending list is MUR 100.00 higher than the recorded batch. Approval remains blocked.",
        "returned": "Reviewer requests the original invoice attachment and corrected service allocation.",
        "bank_mismatch": "Executed instruction and bank statement differ by MUR 150.00. Keep the discrepancy open.",
        "bank_unmatched": "No authorised settlement has been found for the bank debit. Do not mark invoices paid.",
        "disputed": "A MUR 275.00 surcharge is disputed. No waiver is authorised or applied.",
    }
    next_actions = {
        "preparation": "Check received bills, service ownership, attachments and due dates before submission.",
        "provider_check": "Assign the mismatch, obtain a corrected provider listing, then recheck.",
        "approval_pending": "The independent approver must review the batch; no payment can be prepared yet.",
        "returned": "Preparer corrects the evidence and resubmits through the normal approval controls.",
        "settlement_ready": "Finance prepares the instruction with all invoice references; authorisation is still required.",
        "authorisation_pending": "An independent authoriser must verify and sign before bank release.",
        "sent_to_bank": "Await bank execution confirmation. Sending a file alone does not prove payment.",
        "completed": "Inspect the grouped batches, retained remittance lines and matching bank debit.",
        "bank_mismatch": "Investigate the MUR 150.00 difference without rewriting the original instruction.",
        "bank_unmatched": "Assign the debit investigation and obtain bank evidence before any allocation.",
        "disputed": "Finance reviews the request; independent authorisation is required for any adjustment.",
    }
    for index, (provider, ministry, phase, count, title) in enumerate(SPECS, 1):
        reference = f"SIM-BATCH-{index:03d}"
        paid = phase in PAID_STAGES or phase == "sent_to_bank"
        invoices = [
            invoice_example(provider, ministry, f"SIM-INV-{index:03d}-{n + 1:02d}", n + index,
                            settled=paid, surcharge=phase == "disputed" and n == 0)
            for n in range(count)
        ]
        total = sum((inv["total"] for inv in invoices), Decimal("0"))
        item = {
            "reference": reference, "title": title,
            "provider": {"code": provider, "name": PROVIDERS[provider]},
            "ministry": {"code": ministry, "name": MINISTRIES[ministry]},
            "stage": phase, "stage_label": STAGES[phase], "currency": "MUR",
            "tone": "success" if phase == "completed" else "danger" if phase in attention else "info",
            "invoices": invoices, "invoice_count": len(invoices), "total": total,
            "description": (
                "Fictional walkthrough data with indicative charges. These examples are not part of "
                "operational invoice, approval, settlement, bank or financial reporting records."
            ),
            "period_label": f"{invoices[0]['period_start']:%d %b %Y} – {invoices[0]['period_end']:%d %b %Y}",
            "attention": attention.get(phase, ""), "next_action": next_actions[phase],
            "settlement": None, "bank": None, "evidence": [],
        }
        if phase in SETTLEMENT_STAGES:
            item["settlement"] = {
                "reference": f"SIM-SI-{index:03d}", "group_reference": "",
                "status_label": {
                    "settlement_ready": "Illustrative draft — not authorised",
                    "authorisation_pending": "Illustrative authorisation pending",
                    "sent_to_bank": "Illustrative file sent — execution not confirmed",
                }.get(phase, "Illustrative completed settlement — no real transfer"),
                "total": total, "requested_date": date(2026, 9, 8) if paid else date(2026, 11, 20),
                "value_date": date(2026, 9, 8) if phase in PAID_STAGES else None,
                "bank_reference": f"SIM-BANK-EXEC-{index:03d}" if phase in PAID_STAGES else "",
                "remittance_reference": f"SIM-REMIT-{index:03d}",
                "batches": [{"reference": reference, "total": total}],
            }
        if phase in PAID_STAGES | {"bank_unmatched"}:
            actual = total + (Decimal("150.00") if phase == "bank_mismatch" else Decimal("0"))
            item["bank"] = {
                "status_label": {
                    "completed": "Illustrative debit matched",
                    "bank_mismatch": "Illustrative amount mismatch",
                    "bank_unmatched": "Illustrative debit without settlement evidence",
                }[phase],
                "expected": total if phase != "bank_unmatched" else None, "actual": actual,
                "variance": Decimal("150.00") if phase == "bank_mismatch" else Decimal("0") if phase == "completed" else None,
                "transaction_reference": f"SIM-TXN-{index:03d}", "value_date": date(2026, 9, 8),
                "reason": attention.get(phase, "Amount, currency and execution reference agree in this fictional example."),
            }
        item["journey"] = journey_for(item)
        item["evidence"] = [
            {"when": step["when"], "event": step["label"], "role": "Illustrative " + step["role"],
             "note": "Fictional milestone, not a user action, signature or operational audit event."}
            for step in item["journey"] if step["state"] == "done"
        ]
        result.append(item)
    grouped = [result[0], result[8]]
    group_total = sum((item["total"] for item in grouped), Decimal("0"))
    members = [{"reference": item["reference"], "total": item["total"]} for item in grouped]
    for item in grouped:
        item["settlement"].update({
            "reference": "SIM-SI-CEB-GROUP-001", "group_reference": "SIM-GROUP-CEB-001",
            "total": group_total, "batches": members, "remittance_reference": "SIM-REMIT-CEB-GROUP-001",
            "bank_reference": "SIM-BANK-CEB-GROUP-001",
        })
        item["bank"].update({
            "expected": group_total, "actual": group_total,
            "transaction_reference": "SIM-TXN-CEB-GROUP-001",
        })
    return result
