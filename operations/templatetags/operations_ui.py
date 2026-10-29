from django import template
from operations.models import BatchMember
from operations.services import OPENERS, payable
from org.access import can_view, has_role
register = template.Library()


@register.inclusion_tag("operations/invoice_panel.html", takes_context=True)
def invoice_operations(context, invoice):
    user = context["request"].user
    membership = BatchMember.objects.select_related("batch").filter(invoice=invoice, active=True).first()
    return {"invoice": invoice, "payable": payable(invoice),
            "cases": invoice.dispute_cases.all(),
            "batch": membership.batch if membership and can_view(user, membership.batch.org_unit) else None,
            "can_open": has_role(user, invoice.org_unit, OPENERS)}
