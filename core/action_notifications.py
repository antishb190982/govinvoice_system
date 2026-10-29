"""Action alerts for intake, batch preparation and exception ownership.

Approval/payment engines already notify their next responsible roles. This
extends those routes, rather than broadcasting every audit event to everyone.
"""
from django.urls import reverse

from core.notify import notify
from org.access import users_with_role
from org.models import Role

EVENTS = {
    "batch.created", "batch.member_released",
    "dispute.opened", "dispute.finance_reviewed", "dispute.authorised",
    "dispute.rejected", "invoice.created", "invoice.reconciliation_returned",
    "invoice.surcharge_captured", "invoice.surcharge_recorded",
    "import.staged", "import.committed", "reconciliation.reassigned",
    "reconciliation.statement_uploaded",
}
PROCESSORS = [Role.CLERK, Role.UNIT_ADMIN]


def dispatch(event, obj):
    action = event.action
    if action not in EVENTS or obj is None:
        return
    recipients, title, body, link = [], "", "", ""
    if action.startswith("import."):
        recipients = [obj.uploaded_by]
        title = ("Invoices received — review the import" if action == "import.staged"
                 else "Imported invoices need batch preparation")
        body = obj.original_name
        link = reverse("imports:batch", args=[obj.pk]) if action == "import.staged" else reverse("operations:workspace")
    elif action.startswith("batch."):
        roles = PROCESSORS
        if action == "batch.created":
            statuses = list(obj.members.filter(active=True).values_list("invoice__status", flat=True))
            if statuses and all(status == "approved" for status in statuses):
                roles = [Role.FINANCE]
        recipients = users_with_role(obj.org_unit, roles).exclude(pk=event.actor_id)
        title = f"{obj.reference}: " + ("batch preparation required" if action == "batch.created"
                                       else "released invoice needs regrouping")
        link = obj.get_absolute_url()
    elif action.startswith("dispute."):
        roles = [Role.FINANCE]
        if action == "dispute.finance_reviewed":
            roles = [Role.AUTHORISER]
            title = "Dispute decision requires independent authorisation"
        elif action == "dispute.opened":
            title = "New invoice dispute requires finance review"
        elif action == "dispute.authorised":
            if obj.status == "refund_due":
                title = "Approved refund requires recovery and receipt evidence"
            else:
                roles = PROCESSORS
                title = "Dispute resolved — invoice processing can resume"
        else:
            roles = PROCESSORS
            title = "Dispute request declined — review the invoice"
        recipients = users_with_role(obj.org_unit, roles).exclude(pk=event.actor_id)
        if action == "dispute.finance_reviewed":
            recipients = recipients.exclude(pk=obj.opened_by_id)
        link = obj.get_absolute_url()
        body = obj.invoice.reference
    elif action == "reconciliation.reassigned":
        recipients = ([obj.assigned_to] if obj.assigned_to_id else
                      users_with_role(obj.assigned_unit, PROCESSORS + [Role.FINANCE]))
        title = "A reconciliation mismatch has been assigned to you"
        link = reverse("reconciliation:statement", args=[obj.statement_id])
    elif action == "reconciliation.statement_uploaded":
        if not obj.rows.filter(status="unsorted").exists():
            return
        recipients = users_with_role(obj.org_unit, [Role.FINANCE, Role.UNIT_ADMIN]).exclude(pk=event.actor_id)
        title = "Provider statement mismatches require sorting"
        link = obj.get_absolute_url()
    else:
        roles = PROCESSORS
        title = "New invoice requires batch preparation"
        if action == "invoice.reconciliation_returned":
            title = "Invoice returned — correct its reconciliation mismatch"
        elif action.startswith("invoice.surcharge_"):
            if not obj.surcharge_amount or obj.surcharge_reviewed_at:
                return
            roles = [Role.FINANCE]
            title = "Invoice surcharge requires finance assessment"
        recipients = users_with_role(obj.org_unit, roles).exclude(pk=event.actor_id)
        body = obj.reference
        link = (reverse("invoices:surcharge", args=[obj.pk]) if roles == [Role.FINANCE]
                else obj.get_absolute_url())
    notify(recipients, title, body, link=link, dedupe_key=f"action-event-{event.sequence}",
           action_required=True)
