from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_http_methods
from django.core.files.base import ContentFile

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


class AppearanceForm(forms.Form):
    image = forms.FileField(label="Signature image (PNG or JPEG)", required=False)
    pin = forms.CharField(label="Signing PIN", widget=forms.PasswordInput(attrs={"autocomplete": "off"}))
    confirm = forms.BooleanField(label="I confirm this is my signature image")


@login_required
@require_http_methods(["GET", "POST"])
def appearance(request):
    from signatures.appearance import normalise_image

    identity = services.active_identity(request.user)
    if not identity or not identity.is_usable:
        raise PermissionDenied("Set up a valid signing certificate first.")
    if request.method == "GET":
        if not identity.signature_image:
            from django.http import Http404
            raise Http404
        with identity.signature_image.open("rb") as source:
            response = HttpResponse(source.read(), content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    form = AppearanceForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            services.check_pin(request.user, form.cleaned_data["pin"])
            old_hash = identity.signature_image_sha256
            if request.POST.get("action") == "remove":
                identity.signature_image = ""
                identity.signature_image_sha256 = ""
            else:
                upload = form.cleaned_data["image"]
                if not upload:
                    raise ValidationError("Choose a PNG or JPEG signature image.")
                data, digest = normalise_image(upload)
                identity.signature_image.save("signature.png", ContentFile(data), save=False)
                identity.signature_image_sha256 = digest
            identity.save(update_fields=["signature_image", "signature_image_sha256"])
            AuditEvent.record("signature.appearance_changed", obj=identity, actor=request.user,
                              data={"previous_sha256": old_hash, "sha256": identity.signature_image_sha256})
        except (ValidationError, OSError) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else "Signature image storage is unavailable.")
        else:
            messages.success(request, "Signature image updated for future approvals. Historical approvals are unchanged.")
            return redirect("signatures:mine")
    return render(request, "signatures/appearance.html", {"form": form, "identity": identity})


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
        "appearance_form": AppearanceForm(),
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
