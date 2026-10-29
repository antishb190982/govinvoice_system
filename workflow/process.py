"""Read-only process inspection. Existing engines remain the only decision executors."""

from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from invoices.models import Invoice
from org.access import can_view, users_with_role
from org.models import Role
from settlements.models import SettlementInstruction
from signatures.models import SigningIdentity
from workflow import engine
from workflow.models import ApprovalTask


def node(label, role="", state="waiting", current=False, **extra):
    return {"label": label, "role": role, "state": state, "current": current,
            "reasons": [], "url": "", "owners": [], **extra}


def owners(unit, roles, excluded=(), signature=False):
    people = list(users_with_role(unit, roles).exclude(pk__in=[pk for pk in excluded if pk]).order_by("full_name", "pk"))
    ready = set(SigningIdentity.objects.filter(
        user_id__in=[p.pk for p in people], is_active=True, revoked_at__isnull=True,
    ).values_list("user_id", flat=True)) if signature else {p.pk for p in people}
    reasons = []
    if not people:
        reasons.append("No eligible active role holder after scope and separation-of-duties checks.")
    elif signature and not ready:
        reasons.append("No eligible role holder has active signing setup to advance this signed step.")
    return {"owners": people[:10], "owner_count": len(people), "signing_ready": len(ready), "reasons": reasons}


def invoice_process(invoice, viewer):
    tasks = list(invoice.approval_tasks.select_related("decided_by").order_by("round", "order"))
    latest_round = max((task.round for task in tasks), default=0)
    latest = [task for task in tasks if task.round == latest_round]
    terminal = invoice.status in [Invoice.Status.PAID, Invoice.Status.CANCELLED] or (
        invoice.status == Invoice.Status.APPROVED and invoice.amount_total == 0)
    nodes = [node("Invoice recorded", state="complete", date=invoice.created_at)]
    policy = None
    if invoice.status in [Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.DISPUTED]:
        reasons = []
        policy = engine.select_policy(invoice)
        if not policy or not policy.steps.filter(min_amount__lte=invoice.amount_total).exists():
            reasons.append("No active workflow with applicable steps covers this unit, utility and amount.")
        if invoice.status != Invoice.Status.DRAFT:
            reasons.append("The invoice needs correction or a provider response, then resubmission.")
        if not invoice.document:
            reasons.append("Attach the original invoice document before submission.")
        ownership = owners(invoice.org_unit, [Role.CLERK, Role.UNIT_ADMIN])
        reasons.extend(ownership.pop("reasons"))
        nodes.append(node("Review / correction and submission", Role.CLERK.label, "blocked" if reasons else "active",
                          True, reasons=reasons, url=invoice.get_absolute_url(), **ownership))
        if policy:
            for step in policy.steps.order_by("order"):
                if step.min_amount <= invoice.amount_total:
                    nodes.append(node(step.name, step.get_role_display(), signature=step.requires_signature,
                                      sla=step.sla_hours, preview=True, threshold=step.min_amount))
    submitters = set(AuditEvent.objects.filter(action="invoice.submitted", object_type="invoices.invoice",
                                                object_id=str(invoice.pk)).values_list("actor_id", flat=True))
    decided = {task.decided_by_id for task in latest if task.decided_by_id}
    # A returned round is history, not the chain that resubmission will run.
    # Show its recorded decisions in the history table and preview today's policy.
    historical_only = invoice.status in [Invoice.Status.DRAFT, Invoice.Status.RETURNED, Invoice.Status.DISPUTED]
    for task in ([] if historical_only else latest):
        current = task.status == ApprovalTask.Status.PENDING and invoice.status == Invoice.Status.IN_APPROVAL
        state = {"approved": "complete", "pending": "active", "returned": "blocked",
                 "disputed": "blocked", "cancelled": "cancelled"}.get(task.status, "waiting")
        item = node(task.name, task.get_role_display(), state, current, signature=task.requires_signature,
                    sla=task.sla_hours, due_at=task.due_at, date=task.decided_at, task=task,
                    url=reverse("workflow:task", args=[task.pk]), comment=task.comment,
                    decided_by=task.decided_by, escalated=bool(task.escalated_at),
                    escalation_role=task.get_escalation_role_display())
        if current:
            item.update(owners(invoice.org_unit, task.eligible_roles,
                               excluded=submitters | decided | {invoice.created_by_id},
                               signature=task.requires_signature))
            if task.is_overdue:
                item["reasons"].append("The approval SLA deadline has passed.")
                item["state"] = "overdue"
            if item["reasons"] and item["state"] != "overdue":
                item["state"] = "blocked"
        nodes.append(item)
    if invoice.status == Invoice.Status.IN_APPROVAL and not any(n["current"] for n in nodes):
        nodes.append(node("Approval state needs reconciliation", Role.UNIT_ADMIN.label, "blocked", True,
                          reasons=["Invoice is in approval but has no pending task. Ask a unit administrator to investigate."]))

    # The operational copy is part of the invoice journey. It becomes
    # downloadable after approval; missing source or settlement details remain
    # visible without incorrectly blocking a copy that can still be generated.
    from invoices.evidence import approval_evidence
    copy_evidence = approval_evidence(invoice)
    active_member = invoice.batch_memberships.filter(active=True).select_related("batch").first()
    batch_reference = (copy_evidence["batch_reference"] if copy_evidence else
                       active_member.batch.reference if active_member else "Not assigned")
    settlement_reference = copy_evidence["settlement_reference"] if copy_evidence else "Not prepared"
    bank_reference = copy_evidence["bank_reference"] if copy_evidence else "Pending bank settlement"
    processing_started_at = copy_evidence["processing_started_at"] if copy_evidence else (
        active_member.batch.started_at if active_member else None)
    copy_missing = []
    if not copy_evidence:
        copy_missing.append("Final approval has not been recorded; the watermarked copy is not available yet.")
    if not invoice.document:
        copy_missing.append("No original invoice file is attached; the PDF will contain the approval record only.")
    if batch_reference == "Not assigned":
        copy_missing.append("Batch number is not recorded.")
    if not processing_started_at:
        copy_missing.append("Batch processing start date is not recorded.")
    if settlement_reference == "Not prepared":
        copy_missing.append("Settlement instruction has not been prepared.")
    if bank_reference == "Pending bank settlement":
        copy_missing.append("Bank settlement reference is pending until settlement is confirmed.")
    decisions = copy_evidence["decisions"] if copy_evidence else []
    if copy_evidence and not decisions:
        copy_missing.append("Reviewer and approver names or decision dates are not recorded.")
    for decision in decisions:
        if decision["person"] == "Not recorded":
            copy_missing.append(f"Approver for {decision['name']} is not recorded.")
        if not decision["at"]:
            copy_missing.append(f"Approval date for {decision['name']} is not recorded.")
    nodes.append(node(
        "Watermarked invoice copy", "Approved invoice evidence",
        "complete" if copy_evidence else "cancelled" if invoice.status == Invoice.Status.CANCELLED else "waiting",
        watermark_copy=True, copy_ready=bool(copy_evidence),
        url=reverse("invoices:approved_copy", args=[invoice.pk]) if copy_evidence else "",
        date=copy_evidence["approved_at"] if copy_evidence else None,
        processing_started_at=processing_started_at,
        batch_reference=batch_reference, settlement_reference=settlement_reference,
        bank_reference=bank_reference, decisions=decisions, missing_fields=copy_missing,
    ))

    item = invoice.settlement_items.select_related("line__instruction").exclude(
        line__instruction__status=SettlementInstruction.Status.CANCELLED,
    ).order_by("-line__instruction__prepared_at", "-pk").first()
    instruction = item.line.instruction if item else None
    payment_url = instruction.get_absolute_url() if instruction and can_view(viewer, instruction.org_unit) else ""
    if not terminal:
        in_payment = invoice.status in [Invoice.Status.APPROVED, Invoice.Status.SCHEDULED]
        status = instruction.status if instruction else None
        finance_current = in_payment and (not instruction or status == SettlementInstruction.Status.DRAFT)
        auth_current = in_payment and status == SettlementInstruction.Status.PENDING_AUTHORISATION
        bank_current = in_payment and status in [SettlementInstruction.Status.AUTHORISED, SettlementInstruction.Status.SENT]
        phases = [
            ("Prepare and sign payment instruction", Role.FINANCE, finance_current,
             bool(instruction and status != SettlementInstruction.Status.DRAFT), True),
            ("Authorise payment", Role.AUTHORISER, auth_current,
             status in [SettlementInstruction.Status.AUTHORISED, SettlementInstruction.Status.SENT,
                        SettlementInstruction.Status.EXECUTED], True),
            ("Bank / treasury execution and confirmation", Role.FINANCE, bank_current,
             status == SettlementInstruction.Status.EXECUTED, False),
        ]
        for label, role, current, complete, signed in phases:
            part = node(label, role.label, "complete" if complete else "active" if current else "waiting",
                        current, url=payment_url, signature=signed)
            if current:
                excluded = []
                if auth_current and instruction:
                    excluded = [instruction.prepared_by_id] + list(instruction.signings.values_list("user_id", flat=True))
                part.update(owners(instruction.org_unit if instruction else invoice.org_unit,
                                   [role], excluded=excluded, signature=signed))
                if part["reasons"]:
                    part["state"] = "blocked"
                if auth_current and instruction.authorise_by:
                    part["due_at"] = instruction.authorise_by
                    if instruction.authorise_by < timezone.now():
                        part["reasons"].append("The payment authorisation deadline has passed.")
                        part["state"] = "overdue"
                if invoice.status == Invoice.Status.SCHEDULED and not instruction:
                    part["reasons"].append("Settlement status has no active linked instruction; finance must reconcile it.")
                    part["state"] = "blocked"
            nodes.append(part)
        nodes.append(node("Paid / closed", state="waiting"))
    else:
        label = "Cancelled" if invoice.status == Invoice.Status.CANCELLED else \
            "Closed — no payment required" if invoice.amount_total == 0 else "Paid (invoice ledger)"
        nodes.append(node(label, state="cancelled" if invoice.status == Invoice.Status.CANCELLED else "complete",
                          date=invoice.paid_at, payment_reference=invoice.payment_reference))
    current = next((n for n in nodes if n["current"]), nodes[-1])
    from reconciliation.services import report
    reconciliation = report(invoice)
    if not terminal:
        checkpoint = node("Provider statement reconciliation", "Finance / unit administrator",
                          "complete" if reconciliation["valid"] else "blocked",
                          not reconciliation["valid"],
                          url=reverse("reconciliation:index") + "?q=" + invoice.reference,
                          reasons=[] if reconciliation["valid"] else [reconciliation["reason"]])
        evidence = reconciliation["row"]
        if evidence and can_view(viewer, evidence.assigned_unit):
            checkpoint["url"] = evidence.get_absolute_url()
            if evidence.assigned_to and evidence.assigned_to.is_active:
                checkpoint["owners"] = [evidence.assigned_to]
                checkpoint["owner_count"] = 1
        if not reconciliation["valid"]:
            for existing in nodes:
                existing["current"] = False
            current = checkpoint
        nodes.insert(1, checkpoint)
    attention = list(current["reasons"])
    if not terminal and invoice.processing["priority"] in ["overdue", "urgent"]:
        attention.append("Invoice payment timing requires urgent attention.")
    return {"invoice": invoice, "nodes": nodes, "current": current, "attention": attention,
            "needs_attention": bool(attention), "tasks": tasks, "round": latest_round,
            "policy": policy, "instruction": instruction, "instruction_url": payment_url,
            "terminal": terminal, "now": timezone.now()}
