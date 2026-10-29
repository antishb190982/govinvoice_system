"""Department-focused, read-only projections of an authorised process."""

from collections import OrderedDict

from django.core.paginator import Paginator
from django.http import QueryDict

from invoices.models import Invoice
from org.access import active_memberships, scope
from org.models import Role


def department_context(invoice, viewer, nodes):
    memberships = list(active_memberships(viewer).filter(
        org_unit_id__in=invoice.org_unit.ancestor_ids))
    roles = {membership.role for membership in memberships}
    privileged = viewer.is_superuser or bool(roles & {Role.UNIT_ADMIN, Role.AUDITOR})
    focused = []
    for step in nodes:
        task = step.get("task")
        stage_roles = set(task.eligible_roles if task else step.get("stage_roles", []))
        relevant = bool(roles & stage_roles)
        actor = step.get("decided_by")
        acted = bool(actor and actor.pk == viewer.pk)
        assigned_owner = invoice.processing_owner_id or invoice.created_by_id
        assigned = relevant and Role.CLERK in stage_roles and assigned_owner == viewer.pk
        step["your_role"] = relevant
        step["acted_by_you"] = acted
        step["assigned_to_you"] = assigned
        step["handoff"] = step["current"] and not relevant and not acted
        if privileged or relevant or acted or step["current"] or step.get("watermark_copy"):
            focused.append(step)
    units = list(dict.fromkeys(str(m.org_unit) for m in memberships))
    role_names = [str(Role(role).label) for role in Role.values if role in roles]
    return {
        "department_nodes": focused,
        "journey_scope": "All authorised steps" if privileged else ", ".join(role_names) or "Read-only journey",
        "journey_departments": " · ".join(units) if units else "System administrator" if viewer.is_superuser else str(invoice.org_unit),
    }


def batch_journey(batch, viewer, params=None):
    from workflow.process import invoice_process

    invoices = scope(Invoice.objects.filter(
        batch_memberships__batch=batch, batch_memberships__active=True,
    ).select_related("org_unit", "provider", "created_by", "processing_owner", "service_account"), viewer)
    params = (params if params is not None else QueryDict()).copy()
    page = Paginator(invoices.order_by("due_date", "pk"), 20).get_page(params.get("journey_page"))
    processes = [invoice_process(invoice, viewer) for invoice in page]
    stages = OrderedDict()
    for process in processes:
        for step in process["department_nodes"]:
            key = (step["label"], step["role"])
            summary = stages.setdefault(key, {
                "label": step["label"], "role": step["role"], "total": 0,
                "complete": 0, "current": 0, "attention": 0,
            })
            summary["total"] += 1
            summary["complete"] += step["state"] == "complete"
            summary["current"] += step["current"]
            summary["attention"] += step["state"] in {"blocked", "overdue"}
    def page_url(number):
        query = params.copy()
        query["journey_page"] = number
        return "?" + query.urlencode() + "#batch-journey"

    return {"processes": processes, "stages": list(stages.values()), "page": page,
            "total": page.paginator.count,
            "previous_url": page_url(page.previous_page_number()) if page.has_previous() else "",
            "next_url": page_url(page.next_page_number()) if page.has_next() else "",
            "attention_count": sum(p["needs_attention"] for p in processes)}
