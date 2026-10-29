from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from core.utils import paginate
from invoices.models import Invoice
from org.access import has_role, scope, units_for, user_has_any_role
from org.models import Role
from workflow.models import ApprovalTask
from workflow.process import invoice_process


class ProcessFilter(forms.Form):
    q = forms.CharField(required=False, max_length=100, label="Invoice / account / provider")
    org_unit = forms.ModelChoiceField(queryset=None, required=False, empty_label="All my units")
    status = forms.ChoiceField(required=False, choices=[("", "All statuses")] + list(Invoice.Status.choices))
    attention = forms.ChoiceField(required=False, choices=[("", "All invoices"),
                                  ("overdue", "Overdue approval steps"), ("correction", "Returned or disputed")])

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["org_unit"].queryset = units_for(user)
        self.fields["org_unit"].label_from_instance = lambda unit: unit.indented_name


@login_required
def monitor(request):
    form = ProcessFilter(request.GET or {}, user=request.user)
    invoices = scope(Invoice.objects.all(), request.user)
    if not form.is_valid():
        return render(request, "workflow/processes.html", {
            "form": form, "invalid_filters": True,
            "can_configure": user_has_any_role(request.user, [Role.UNIT_ADMIN]),
        })
    data = form.cleaned_data
    if data.get("org_unit"):
        invoices = invoices.filter(org_unit__path__startswith=data["org_unit"].path)
    if data.get("q"):
        q = data["q"]
        invoices = invoices.filter(Q(reference__icontains=q) | Q(invoice_number__icontains=q) |
                                   Q(provider__name__icontains=q) | Q(service_account__account_number__icontains=q))
    if data.get("status"):
        invoices = invoices.filter(status=data["status"])
    if data.get("attention") == "overdue":
        invoices = invoices.filter(status=Invoice.Status.IN_APPROVAL,
                                    approval_tasks__status=ApprovalTask.Status.PENDING,
                                    approval_tasks__due_at__lt=timezone.now()).distinct()
    elif data.get("attention") == "correction":
        invoices = invoices.filter(status__in=[Invoice.Status.RETURNED, Invoice.Status.DISPUTED])
    # scope() is DISTINCT. Clear model ordering so its timestamp does not
    # become an extra GROUP BY key and split each status into one-row groups.
    counts = dict(invoices.order_by().values("status").annotate(n=Count("pk", distinct=True))
                  .values_list("status", "n"))
    summary = []
    for key, label in Invoice.Status.choices:
        params = request.GET.copy()
        params.pop("page", None)
        params["status"] = key
        summary.append({"label": label, "key": key, "count": counts.get(key, 0),
                        "url": reverse("workflow:processes") + "?" + params.urlencode()})
    page = paginate(request, invoices.select_related("provider", "org_unit", "created_by")
                    .order_by("due_date", "pk"), 20)
    return render(request, "workflow/processes.html", {
        "form": form, "summary": summary, "page": page,
        "processes": [invoice_process(invoice, request.user) for invoice in page],
        "can_configure": user_has_any_role(request.user, [Role.UNIT_ADMIN]),
    })


@login_required
def invoice_map(request, pk):
    invoice = get_object_or_404(scope(Invoice.objects.select_related(
        "provider", "org_unit", "created_by", "service_account"), request.user), pk=pk)
    context = invoice_process(invoice, request.user)
    context["can_configure"] = has_role(request.user, invoice.org_unit, [Role.UNIT_ADMIN])
    return render(request, "workflow/process.html", context)
