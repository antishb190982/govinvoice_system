from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.files import validate_upload
from core.models import AuditEvent
from signatures import ca, services
from signatures.models import SigningIdentity


class EnrollForm(forms.Form):
    pin = forms.CharField(label="Choose a signing PIN", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                          help_text=f"At least {services.MIN_PIN_LENGTH} characters. Separate from your password; "
                                    "you type it every time you sign.")
    pin_confirm = forms.CharField(label="Repeat PIN", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password = forms.CharField(label="Your account password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def clean(self):
        d = super().clean()
        if d.get("pin") != d.get("pin_confirm"):
            raise ValidationError("The PINs do not match.")
        if d.get("pin"):
            services.validate_pin(d["pin"])
        return d


class ImportForm(EnrollForm):
    p12 = forms.FileField(label="Certificate file (.p12 / .pfx)")
    p12_password = forms.CharField(label="Certificate file password", required=False,
                                   widget=forms.PasswordInput(attrs={"autocomplete": "off"}))


class VerifyForm(forms.Form):
    document = forms.FileField(label="Signed PDF")


@login_required
def my_identity(request):
    user = request.user
    enroll_form = EnrollForm(request.POST if request.POST.get("form") == "enroll" else None)
    import_form = ImportForm(request.POST if request.POST.get("form") == "import" else None,
                             request.FILES if request.POST.get("form") == "import" else None)
    if request.method == "POST":
        form = enroll_form if request.POST.get("form") == "enroll" else import_form
        if form.is_valid():
            if not user.check_password(form.cleaned_data["password"]):
                form.add_error("password", "Wrong password.")
            else:
                try:
                    if form is enroll_form:
                        services.enroll(user, form.cleaned_data["pin"])
                    else:
                        f = form.cleaned_data["p12"]
                        if f.size > 64 * 1024:
                            raise ValidationError("That file is too large to be a certificate.")
                        services.import_pkcs12(user, f.read(), form.cleaned_data["p12_password"], form.cleaned_data["pin"])
                    messages.success(request, "Your signing certificate is ready.")
                    return redirect("signatures:mine")
                except ValidationError as exc:
                    form.add_error(None, exc)
    return render(request, "signatures/mine.html", {
        "identity": services.active_identity(user),
        "history": SigningIdentity.objects.filter(user=user),
        "enroll_form": enroll_form, "import_form": import_form,
    })


@login_required
@require_POST
def revoke_mine(request):
    identity = services.active_identity(request.user)
    if identity is None:
        raise PermissionDenied
    services.revoke(identity, request.POST.get("reason", "")[:200] or "Revoked by holder", request.user)
    messages.success(request, "Certificate revoked. Enrol again to sign.")
    return redirect("signatures:mine")


@login_required
def verify(request):
    form = VerifyForm(request.POST or None, request.FILES or None)
    reports = None
    if request.method == "POST" and form.is_valid():
        f = form.cleaned_data["document"]
        try:
            _, digest = validate_upload(f, {"pdf"})
            reports = services.validate_pdf(f.read())
            AuditEvent.record("signature.pdf_verified", data={"sha256": digest, "signatures": len(reports),
                                                              "all_ok": all(r["ok"] for r in reports)})
            if not reports:
                messages.warning(request, "This PDF contains no digital signatures.")
        except ValidationError as exc:
            form.add_error("document", exc)
    return render(request, "signatures/verify.html", {"form": form, "reports": reports})


@login_required
def ca_certificate(request):
    from cryptography.hazmat.primitives import serialization

    pem = ca.ca_certificate().public_bytes(serialization.Encoding.PEM)
    response = HttpResponse(pem, content_type="application/x-pem-file")
    response["Content-Disposition"] = 'attachment; filename="govbills-signing-ca.pem"'
    return response
