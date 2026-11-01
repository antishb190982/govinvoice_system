"""Authenticated, read-only training over versioned private App Storage assets."""
import hashlib
import json
import logging
import re
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import render
from django.views.decorators.http import require_safe

from core.app_storage import PrivateAppStorage
from org.access import active_memberships

logger = logging.getLogger(__name__)
CATALOG = Path(__file__).resolve().parent.parent / "docs" / "training-catalog.json"


def catalog_for(user):
    if not user.is_active:
        raise PermissionDenied
    catalog = json.loads(CATALOG.read_text(encoding="utf8"))
    roles = set(active_memberships(user).values_list("role", flat=True))
    if user.is_superuser:
        roles.add("sysadmin")
    for manual in catalog["manuals"]:
        manual["recommended"] = manual["slug"] in roles
    return catalog


@login_required
@require_safe
def training(request):
    catalog = catalog_for(request.user)
    manuals = catalog["manuals"]
    role = request.GET.get("role", "")
    if role and role not in {item["slug"] for item in manuals}:
        raise Http404("Unknown training role")
    visible = [manual for manual in manuals if not role or manual["slug"] == role]
    visible.sort(key=lambda item: (not item["recommended"], item["title"]))
    return render(request, "core/training_library.html", {
        "manuals": visible, "roles": manuals, "selected_role": role, "notice": catalog["notice"]})


@login_required
@require_safe
def training_detail(request, role):
    catalog = catalog_for(request.user)
    manual = next((item for item in catalog["manuals"] if item["slug"] == role), None)
    if not manual:
        raise Http404("Unknown training role")
    return render(request, "core/training_detail.html", {
        "manual": manual, "manuals": catalog["manuals"], "notice": catalog["notice"]})


def byte_range(value, size):
    """Single bounded byte range, including suffix ranges, for native seeking."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value or "")
    if not match or not size or not any(match.groups()):
        raise ValueError("Invalid media range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid suffix range")
        return max(0, size-suffix), size-1
    start = int(first)
    end = min(int(last), size-1) if last else size-1
    if start >= size or start > end:
        raise ValueError("Range outside media")
    return start, end


@login_required
@require_safe
def training_media(request, role, kind):
    catalog = catalog_for(request.user)
    manual = next((item for item in catalog["manuals"] if item["slug"] == role), None)
    if not manual or kind not in {"video", "webm", "captions", "poster"}:
        raise Http404("Unknown training media")
    asset = manual["assets"][kind]
    size = asset["size"]
    etag = f'"{asset["sha256"]}"'
    headers = {
        "Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff", "Vary": "Cookie",
    }
    if request.headers.get("If-None-Match") == etag and not request.headers.get("Range"):
        return HttpResponseNotModified(headers=headers)
    bounds = None
    range_header = request.headers.get("Range")
    if range_header and request.headers.get("If-Range", etag) == etag:
        try:
            bounds = byte_range(range_header, size)
        except ValueError:
            return HttpResponse(status=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    status = 206 if bounds else 200
    if bounds:
        start, end = bounds
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end-start+1)})
    else:
        headers["Content-Length"] = str(size)
    extension = {"video": "mp4", "webm": "webm", "captions": "vtt", "poster": "jpg"}[kind]
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="GovBills-{role}-manual.{extension}"'
    if request.method == "HEAD":
        return HttpResponse(content_type=asset["content_type"], status=status, headers=headers)
    try:
        with PrivateAppStorage().open(asset["name"], "rb") as stream:
            content = stream.read()
        # Never serve an incomplete or silently substituted training file.
        if len(content) != size or hashlib.sha256(content).hexdigest() != asset["sha256"]:
            raise OSError("Training asset does not match its catalog")
        if bounds:
            content = content[start:end+1]
        return HttpResponse(content, content_type=asset["content_type"], status=status, headers=headers)
    except Exception:
        logger.exception("Training media unavailable for role %s and kind %s", role, kind)
        return HttpResponse("The video is temporarily unavailable. The written manual is still available.",
            status=503, content_type="text/plain", headers={"Cache-Control": "no-store"})
