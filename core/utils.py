import csv

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse

from org.access import user_has_any_role


def require_any_role(user, roles):
    if not user_has_any_role(user, roles):
        raise PermissionDenied


def paginate(request, queryset, per_page=25):
    page = Paginator(queryset, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    page.querystring = params.urlencode()
    return page


def _safe_cell(value):
    """Neutralise spreadsheet formula injection in exported text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(text) else text


def _is_number(text):
    try:
        float(text.replace(",", ""))
        return True
    except ValueError:
        return False


def csv_response(filename, header, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe_cell(v) for v in row])
    return response


def file_response(field_file, filename, content_type="application/octet-stream", inline=False):
    from django.http import FileResponse

    response = FileResponse(field_file.open("rb"), content_type=content_type, as_attachment=not inline,
                            filename=filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
