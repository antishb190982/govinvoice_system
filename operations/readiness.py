"""Live preparation guidance over canonical records, never a stored clearance."""
from django.core.exceptions import PermissionDenied, ValidationError
from org.access import has_role
from org.models import Role
from workflow.engine import submission_plan


def batch_readiness(batch, members, user):
    active = [member for member in members if member.active]
    pending = [member for member in active if member.invoice.status in ("draft", "returned", "disputed")]
    eligible = bool(pending) and not batch.cancelled_at and has_role(
        user, batch.org_unit, [Role.CLERK, Role.UNIT_ADMIN])
    issues = []
    ready_count = 0
    for member in active:
        invoice = member.invoice
        reasons = []
        if invoice.provider_id != batch.provider_id or invoice.currency != batch.currency:
            reasons.append("Provider or currency no longer matches this batch.")
        if not invoice.org_unit.path.startswith(batch.org_unit.path) or (
                invoice.service_account.org_unit_id != invoice.org_unit_id):
            reasons.append("Invoice and service-account allocation must match the batch ministry.")
        try:
            if invoice.status in ("draft", "returned", "disputed"):
                # An approver can see blockers without pretending to be the preparer.
                submission_plan(invoice, user if eligible else batch.created_by)
            elif invoice.status in ("paid", "scheduled"):
                pass
            elif invoice.status in ("approved", "in_approval"):
                from reconciliation.services import require_reconciled
                require_reconciled(invoice, lock=False)
            else:
                reasons.append("This invoice is not eligible for batch submission.")
        except ValidationError as exc:
            reasons.extend(exc.messages)
        except PermissionDenied:
            reasons.append("A ministry clerk or administrator must submit this invoice.")
        if reasons:
            issues.append({"invoice": invoice, "reasons": list(dict.fromkeys(reasons))})
        else:
            ready_count += 1
    ready = eligible and bool(active) and not issues
    if batch.cancelled_at:
        stage, action = "Cancelled", "View the retained history; this batch cannot be submitted."
    elif issues:
        stage, action = "Needs attention", "Resolve the listed issues, then check readiness again."
    elif pending:
        stage, action = "Ready for submission", "Submit the whole batch for independent approval."
    else:
        stage, action = batch.lifecycle, "Follow the approval or settlement action for your assigned role."
    return {
        "ready": ready, "eligible": eligible, "invoice_count": len(active),
        "ready_count": ready_count, "blocked_count": len(issues), "issues": issues,
        "stage": stage, "next_action": action,
        "checks": [
            {"label": "Active invoices available", "passed": bool(active)},
            {"label": "Invoice evidence, allocation, reconciliation and workflow checks", "passed": bool(active) and not issues},
            {"label": "Your role can submit this batch", "passed": eligible},
        ],
    }
