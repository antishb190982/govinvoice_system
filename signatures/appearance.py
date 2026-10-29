"""Validated private signature images; historical signatures retain their snapshot."""

import hashlib
import io
import warnings

from django.core.exceptions import ValidationError
from PIL import Image, ImageOps


def normalise_image(upload):
    if upload.size > 1024 * 1024:
        raise ValidationError("Use a PNG or JPEG signature image no larger than 1 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            upload.seek(0)
            image = Image.open(upload)
            if image.format not in {"PNG", "JPEG"} or image.width * image.height > 2_000_000:
                raise ValueError("Unsupported signature image")
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((1200, 400))
            output = io.BytesIO()
            image.save(output, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("Upload a valid PNG or JPEG, at most 2 million pixels.") from exc
    data = output.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def signature_appearance(signature):
    """Only display a verified, hash-bound image captured at signing time."""
    from signatures.services import verify_signature

    if not signature.appearance_image:
        return None, "Signature image not recorded at approval"
    valid, findings = verify_signature(signature)
    signed_hash = signature.payload.get("signature_appearance", {}).get("sha256")
    if not valid or not signed_hash or signed_hash != signature.appearance_sha256:
        raise ValidationError("The recorded signature image is not bound to valid approval evidence.")
    try:
        with signature.appearance_image.open("rb") as source:
            data = source.read(2 * 1024 * 1024)
    except OSError as exc:
        raise ValidationError("The recorded signature image is unavailable.") from exc
    if hashlib.sha256(data).hexdigest() != signed_hash:
        raise ValidationError("The recorded signature image no longer matches its signed SHA-256.")
    return data, ""
