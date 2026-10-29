from urllib.parse import urlencode

from django import forms
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from invoices.models import Invoice
from org.access import unit_filter
from org.models import OrgUnit
from providers.models import Provider, ServiceAccount

INVOICE_STATUSES = [
    ("", "Any invoicing status"),
    ("no_invoices", "No invoices recorded"),
    ("no_invoice_this_month", "No invoice issued this month"),
    ("unpaid", "Unpaid"),
    ("overdue", "Overdue"),
    ("in_approval", "Awaiting approval"),
    ("settled", "Settled"),
]


class AccountRegisterFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search", max_length=120)
    provider = forms.ChoiceField(required=False, label="Utility provider")
    ministry = forms.ChoiceField(required=False, label="Ministry")
    service_type = forms.ChoiceField(required=False, label="Service")
    status = forms.ChoiceField(required=False, label="Account status")
    invoicing_status = forms.ChoiceField(required=False, label="Invoicing status",
                                          choices=INVOICE_STATUSES)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accessible = ServiceAccount.objects.filter(unit_filter(user)).order_by()
        provider_ids = accessible.values_list("provider_id", flat=True).distinct()
        self.fields["provider"].choices = [("", "All utility providers")] + [
            (str(pk), label) for pk, label in Provider.objects.filter(pk__in=provider_ids)
            .order_by("name").values_list("pk", "name")]

        unit_rows = list(accessible.values_list("org_unit__path", flat=True).distinct())
        ministries = [unit for unit in OrgUnit.objects.filter(kind=OrgUnit.Kind.MINISTRY).order_by("name")
                      if any(path.startswith(unit.path) for path in unit_rows)]
        self.fields["ministry"].choices = [("", "All ministries")] + [
            (str(unit.pk), f"{unit.code} · {unit.name}") for unit in ministries]
        services = accessible.exclude(service_type="").values_list("service_type", flat=True).distinct().order_by("service_type")
        self.fields["service_type"].choices = [("", "All services")] + [(value, value) for value in services]
        self.fields["status"].choices = [("", "All account statuses")] + list(ServiceAccount.Status.choices)


def filtered_accounts(user, params=None):
    params = params or {}
    rows = ServiceAccount.objects.select_related("provider", "org_unit").filter(unit_filter(user))
    bound_params = params.copy() if hasattr(params, "copy") else dict(params or {})
    if not bound_params.get("provider") and bound_params.get("utility_provider"):
        bound_params["provider"] = bound_params["utility_provider"]
    form = AccountRegisterFilterForm(user, bound_params)
    values = {}
    today = timezone.localdate()
    month_start = today.replace(day=1)
    invoices = Invoice.objects.filter(service_account_id=OuterRef("pk"))
    rows = rows.annotate(
        has_invoices=Exists(invoices),
        has_invoice_this_month=Exists(invoices.filter(issue_date__gte=month_start)),
        has_unpaid=Exists(invoices.filter(status__in=Invoice.UNPAID)),
        has_overdue=Exists(invoices.filter(status__in=Invoice.UNPAID, due_date__lt=today)),
        has_approval=Exists(invoices.filter(status=Invoice.Status.IN_APPROVAL)),
        has_settled=Exists(invoices.filter(status=Invoice.Status.PAID)),
    )
    if form.is_valid():
        values = form.cleaned_data
        if values.get("provider"):
            rows = rows.filter(provider_id=values["provider"])
        if values.get("ministry"):
            ministry = OrgUnit.objects.get(pk=values["ministry"], kind=OrgUnit.Kind.MINISTRY)
            rows = rows.filter(org_unit__path__startswith=ministry.path)
        if values.get("service_type"):
            rows = rows.filter(service_type=values["service_type"])
        if values.get("status"):
            rows = rows.filter(status=values["status"])
        q = values.get("q", "").strip()
        if q:
            from django.db.models import Q
            rows = rows.filter(Q(account_number__icontains=q) | Q(provider__name__icontains=q) |
                Q(owner__icontains=q) | Q(purpose__icontains=q) | Q(service_type__icontains=q) |
                Q(premises__icontains=q))

        flags = {
            "no_invoices": ~Exists(invoices),
            "no_invoice_this_month": ~Exists(invoices.filter(issue_date__gte=month_start)),
            "unpaid": Exists(invoices.filter(status__in=Invoice.UNPAID)),
            "overdue": Exists(invoices.filter(status__in=Invoice.UNPAID, due_date__lt=today)),
            "in_approval": Exists(invoices.filter(status=Invoice.Status.IN_APPROVAL)),
            "settled": Exists(invoices.filter(status=Invoice.Status.PAID)),
        }
        if values.get("invoicing_status") in flags:
            rows = rows.filter(flags[values["invoicing_status"]])
    return rows.distinct(), form, values


def account_register_url(params=None, **changes):
    values = dict(params or {})
    values.update(changes)
    values = {key: value for key, value in values.items() if value not in (None, "")}
    query = urlencode(values)
    return reverse("providers:accounts") + (f"?{query}" if query else "")


def dashboard_for(user, params=None):
    rows, form, values = filtered_accounts(user, params)
    base_params = {key: value for key, value in values.items() if value}

    def link(**changes):
        return account_register_url(base_params, **changes)

    status_counts = {status: rows.filter(status=status).count() for status, _ in ServiceAccount.Status.choices}
    count = rows.count()
    return {
        "account_filter_form": form,
        "filter_values": values,
        "account_rows": rows,
        "account_total": count,
        "account_status_breakdown": [
            {"label": label, "count": status_counts[status],
             "url": link(status=status)}
            for status, label in ServiceAccount.Status.choices],
        "account_dashboard_cards": [
            {"label": "Matching accounts", "count": count, "url": link()},
            {"label": "No invoice recorded", "count": rows.filter(has_invoices=False).count(),
             "url": link(invoicing_status="no_invoices")},
            {"label": "No invoice this month", "count": rows.filter(has_invoice_this_month=False).count(),
             "url": link(invoicing_status="no_invoice_this_month")},
            {"label": "Unpaid", "count": rows.filter(has_unpaid=True).count(),
             "url": link(invoicing_status="unpaid")},
            {"label": "Overdue", "count": rows.filter(has_overdue=True).count(),
             "url": link(invoicing_status="overdue")},
            {"label": "Awaiting approval", "count": rows.filter(has_approval=True).count(),
             "url": link(invoicing_status="in_approval")},
            {"label": "Settled", "count": rows.filter(has_settled=True).count(),
             "url": link(invoicing_status="settled")},
        ],
    }
