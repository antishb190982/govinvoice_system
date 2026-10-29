"""System-admin configuration and inspection of inbound e-bill deliveries."""
from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import AuditEvent
from core.utils import paginate
from integrations.console import sysadmin_required
from integrations.inbound import process_delivery, relay_problem
from integrations.inbound_models import EbillDelivery, EbillMailbox, EbillSender
from integrations.models import ApiClient
from providers.models import Provider


class MailboxForm(forms.ModelForm):
    prefix = "mailbox"
    class Meta:
        model = EbillMailbox
        fields = ["name", "address", "relay_client", "enabled", "auto_stage"]
        labels = {"address": "Dedicated mailbox address", "relay_client": "Trusted mail relay client",
                  "enabled": "Accept relayed messages", "auto_stage": "Automatically stage safe, authorised attachments"}
        help_texts = {
            "relay_client": "Create a dedicated API client with only the Receive e-bills scope and a non-superuser clerk or unit-administrator processing identity.",
            "enabled": "Keep off until your mail administrator has configured the authenticated relay.",
            "auto_stage": "Staging never commits invoices, approves batches or executes payments.",
        }

    def clean_address(self):
        return self.cleaned_data["address"].strip().lower()

    def clean(self):
        data = super().clean()
        client = data.get("relay_client")
        if data.get("enabled") and client and (problem := relay_problem(client)):
            self.add_error("relay_client", problem)
        if data.get("enabled") and not client:
            self.add_error("relay_client", "Select a trusted relay client before enabling intake.")
        return data


class SenderForm(forms.ModelForm):
    prefix = "sender"
    class Meta:
        model = EbillSender
        fields = ["address", "provider", "enabled"]
        labels = {"address": "Exact authorised sender address", "provider": "Utility provider"}
        help_texts = {"address": "No domain wildcards or display-name matches. One sender maps to one provider per mailbox."}

    def __init__(self, *args, mailbox, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.mailbox = mailbox
        self.fields["provider"].queryset = Provider.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.provider_id))

    def clean_address(self):
        value = self.cleaned_data["address"].strip().lower()
        if EbillSender.objects.filter(mailbox=self.instance.mailbox, address=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This mailbox already has a rule for that sender. Edit the existing rule.")
        return value


@sysadmin_required
def incoming(request, pk=None, rule_pk=None):
    mailbox = get_object_or_404(EbillMailbox, pk=pk) if pk else None
    rule = get_object_or_404(EbillSender, mailbox=mailbox, pk=rule_pk) if rule_pk else None
    mailbox_form = MailboxForm(instance=mailbox)
    sender_form = SenderForm(mailbox=mailbox, instance=rule) if mailbox else None
    status = 200
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_mailbox":
            mailbox_form = MailboxForm(request.POST, instance=mailbox)
            if mailbox_form.is_valid():
                saved = mailbox_form.save()
                AuditEvent.record("mail.mailbox_configured", obj=saved, actor=request.user, data={
                    "address": saved.address, "enabled": saved.enabled, "auto_stage": saved.auto_stage,
                    "relay_client_id": saved.relay_client_id})
                messages.success(request, "Mailbox settings saved. Receiving mail still requires your mail server's relay configuration.")
                return redirect("console:incoming_mailbox", pk=saved.pk)
            status = 400
        elif action == "save_sender" and mailbox:
            sender_form = SenderForm(request.POST, mailbox=mailbox, instance=rule)
            if sender_form.is_valid():
                saved = sender_form.save()
                AuditEvent.record("mail.sender_configured", obj=saved, actor=request.user, data={
                    "mailbox_id": mailbox.pk, "sender": saved.address,
                    "provider_id": saved.provider_id, "enabled": saved.enabled})
                messages.success(request, "Authorised sender rule saved.")
                return redirect("console:incoming_mailbox", pk=mailbox.pk)
            status = 400
        else:
            return HttpResponseBadRequest("Unknown mailbox action.")
    deliveries = EbillDelivery.objects.select_related("mailbox", "provider")
    if mailbox:
        deliveries = deliveries.filter(mailbox=mailbox)
    stats = {r["state"]: r["total"] for r in deliveries.values("state").annotate(total=Count("pk")).order_by()}
    selected_state = request.GET.get("state", "")
    if selected_state in EbillDelivery.State.values:
        deliveries = deliveries.filter(state=selected_state)
    return render(request, "integrations/incoming.html", {
        "tab": "incoming", "hide_provider_tabs": True, "mailbox": mailbox,
        "mailboxes": EbillMailbox.objects.select_related("relay_client").annotate(
            sender_count=Count("senders", filter=Q(senders__enabled=True), distinct=True)),
        "mailbox_form": mailbox_form, "sender_form": sender_form, "editing_rule": rule,
        "senders": mailbox.senders.select_related("provider") if mailbox else [],
        "stats": stats, "delivery_total": sum(stats.values()), "selected_state": selected_state,
        "states": EbillDelivery.State.choices, "page": paginate(request, deliveries, 25),
    }, status=status)


@sysadmin_required
@require_POST
def toggle_sender(request, pk, rule_pk):
    rule = get_object_or_404(EbillSender, pk=rule_pk, mailbox_id=pk)
    rule.enabled = not rule.enabled
    rule.save(update_fields=["enabled", "updated_at"])
    AuditEvent.record("mail.sender_toggled", obj=rule, actor=request.user,
                      data={"mailbox_id": pk, "enabled": rule.enabled, "provider_id": rule.provider_id})
    return redirect("console:incoming_mailbox", pk=pk)


@sysadmin_required
def delivery(request, pk):
    record = get_object_or_404(EbillDelivery.objects.select_related("mailbox", "provider", "relay_client"), pk=pk)
    return render(request, "integrations/incoming_delivery.html", {
        "tab": "incoming", "hide_provider_tabs": True, "delivery": record,
        "batches": record.import_batches.all(),
        "can_retry": record.state != "staged" and record.authentication_passed and record.attachments_clean,
    })


@sysadmin_required
@require_POST
def retry(request, pk):
    record = get_object_or_404(EbillDelivery, pk=pk)
    result = process_delivery(record.pk, manual=True, actor=request.user)
    if result.state == "staged":
        messages.success(request, "Attachments are staged for invoice review. No invoices were approved or paid.")
    else:
        messages.warning(request, result.reason)
    return redirect("console:incoming_delivery", pk=pk)


@sysadmin_required
def download(request, pk):
    record = get_object_or_404(EbillDelivery, pk=pk)
    response = FileResponse(record.raw_message.open("rb"), as_attachment=True,
                            filename=f"ebill-message-{record.pk}.eml", content_type="message/rfc822")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = "private, no-store"
    return response
