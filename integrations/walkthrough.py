"""Administrator-only presentation examples, isolated from all financial tables."""
import csv
import io
from decimal import Decimal

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from core.walkthrough_examples import AS_OF, MINISTRIES, PROVIDERS, STAGES, scenarios
from integrations.console import sysadmin_required


def decorated_scenarios():
    items = scenarios()
    for item in items:
        item["detail_url"] = reverse("console:walkthrough_detail", args=[item["reference"]])
        item["remittance_url"] = (
            reverse("console:walkthrough_remittance", args=[item["reference"]])
            if item["settlement"] else ""
        )
        item["bank_csv_url"] = (
            reverse("console:walkthrough_bank", args=[item["reference"]]) if item["bank"] else ""
        )
        for invoice in item["invoices"]:
            invoice["pdf_url"] = reverse("console:walkthrough_invoice", args=[item["reference"], invoice["number"]])
    return items


def get_scenario(reference):
    for item in decorated_scenarios():
        if item["reference"] == reference:
            return item
    raise Http404


def private_response(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@sysadmin_required
@require_GET
def index(request):
    items = decorated_scenarios()
    provider = request.GET.get("provider", "")
    ministry = request.GET.get("ministry", "")
    stage = request.GET.get("stage", "")
    search = request.GET.get("q", "").strip()[:100]
    for selected, choices in ((provider, PROVIDERS), (ministry, MINISTRIES), (stage, STAGES)):
        if selected and selected not in choices:
            raise Http404
    if provider:
        items = [i for i in items if i["provider"]["code"] == provider]
    if ministry:
        items = [i for i in items if i["ministry"]["code"] == ministry]
    if stage:
        items = [i for i in items if i["stage"] == stage]
    if search:
        def searchable(item):
            values = [item["reference"], item["title"], item["provider"]["name"], item["ministry"]["name"]]
            values.extend(f"{inv['number']} {inv['account']}" for inv in item["invoices"])
            return " ".join(values).casefold()
        items = [i for i in items if search.casefold() in searchable(i)]
    summary = {
        "batches": len(items), "invoices": sum(i["invoice_count"] for i in items),
        "total": sum((i["total"] for i in items), Decimal("0")),
        "completed": sum(i["stage"] == "completed" for i in items),
        "pending": sum(i["stage"] != "completed" and not i["attention"] for i in items),
        "exceptions": sum(bool(i["attention"]) for i in items),
    }
    return private_response(render(request, "integrations/walkthrough_list.html", {
        "scenarios": items, "summary": summary, "currency": "MUR", "as_of": AS_OF, "tab": "walkthrough",
        "providers": [{"value": k, "label": v} for k, v in PROVIDERS.items()],
        "ministries": [{"value": k, "label": v} for k, v in MINISTRIES.items()],
        "stages": [{"value": k, "label": v} for k, v in STAGES.items()],
        "selected_provider": provider, "selected_ministry": ministry, "selected_stage": stage, "search": search,
    }))


@sysadmin_required
@require_GET
def detail(request, reference):
    return private_response(render(request, "integrations/walkthrough_detail.html", {
        "scenario": get_scenario(reference), "as_of": AS_OF, "tab": "walkthrough",
    }))


def csv_response(filename, headings, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headings)
    writer.writerows(rows)
    response = HttpResponse(stream.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="SIMULATION-ONLY-{filename}.csv"'
    return private_response(response)


@sysadmin_required
@require_GET
def remittance(request, reference):
    item = get_scenario(reference)
    settlement = item["settlement"]
    if not settlement:
        raise Http404
    members = {v["reference"] for v in settlement["batches"]}
    rows = [
        [True, "ILLUSTRATIVE FORMAT - NOT A BANK SUBMISSION FILE", settlement["reference"],
         settlement["group_reference"], batch["reference"], item["provider"]["code"],
         invoice["account"], invoice["number"], invoice["total"], "MUR",
         settlement["remittance_reference"], settlement["bank_reference"]]
        for batch in scenarios() if batch["reference"] in members for invoice in batch["invoices"]
    ]
    return csv_response(reference + "-remittance", [
        "simulation_only", "format_notice", "settlement_reference", "settlement_group", "batch_reference",
        "provider", "account", "invoice_reference", "amount", "currency", "remittance_reference", "bank_reference",
    ], rows)


@sysadmin_required
@require_GET
def bank_csv(request, reference):
    item = get_scenario(reference)
    bank = item["bank"]
    if not bank:
        raise Http404
    return csv_response(reference + "-bank-example", [
        "simulation_only", "format_notice", "transaction_reference", "settlement_reference",
        "bank_reference", "value_date", "debit", "currency", "illustrative_result",
    ], [[True, "ILLUSTRATIVE FORMAT - NOT BANK EVIDENCE", bank["transaction_reference"],
         item["settlement"]["reference"] if item["settlement"] else "",
         item["settlement"]["bank_reference"] if item["settlement"] else "",
         bank["value_date"], bank["actual"], "MUR", bank["status_label"]]])


@sysadmin_required
@require_GET
def invoice_pdf(request, reference, number):
    item = get_scenario(reference)
    invoice = next((v for v in item["invoices"] if v["number"] == number), None)
    if not invoice:
        raise Http404
    output = io.BytesIO()
    doc = canvas.Canvas(output, pagesize=A4)
    doc.setTitle(number + " — simulation only")
    doc.setFont("Helvetica-Bold", 16)
    doc.drawString(40, 800, "SIMULATION ONLY - DO NOT PAY")
    doc.setFont("Helvetica", 10)
    doc.drawString(40, 780, "Fictional GovBills example. Not a provider-issued bill or signed operational copy.")
    lines = [
        f"Provider illustration: {item['provider']['name']}",
        f"Batch: {reference} | Invoice: {number}",
        f"Account: {invoice['account']} | Premises: {invoice['site']}",
        f"Service: {invoice['service']} | Meter: {invoice['meter']}",
        f"Period: {invoice['period_start']} to {invoice['period_end']}",
        f"Issued: {invoice['issue_date']} | Received: {invoice['received_date']} | Due: {invoice['due_date']}",
        f"Usage: {invoice['quantity']} {invoice['unit']} (illustrative)",
    ]
    if invoice["previous_reading"] is not None:
        lines.append(f"Readings: {invoice['previous_reading']} to {invoice['current_reading']}")
    for row in invoice["lines"]:
        lines.append(f"{row['description'][:65]}: MUR {row['amount']:,.2f}")
    lines += [
        f"Net: MUR {invoice['net']:,.2f} | Tax: MUR {invoice['tax']:,.2f}",
        f"Surcharge (included in total): MUR {invoice['surcharge']:,.2f}",
        f"Total: MUR {invoice['total']:,.2f}",
        f"Illustrative scenario: {item['stage_label']}",
    ]
    if item["settlement"]:
        lines += [
            "SIMULATED settlement reference: " + item["settlement"]["reference"],
            "SIMULATED bank reference: " + (item["settlement"]["bank_reference"] or "Not recorded"),
        ]
    lines += [
        "No real user approval, signature, authorisation or transfer is represented.",
        "Indicative charges only; not official tariffs. Not digitally signed.",
    ]
    for index, value in enumerate(lines):
        doc.drawString(40, 750 - index * 23, value)
    doc.save()
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="SIMULATION-ONLY-{number}.pdf"'
    return private_response(response)
