"""Live, permission-scoped attention items for the header notification centre."""
from django.urls import reverse

from core.work_queries import overdue_sources, row_for


def local_link(value):
    value = str(value or "")
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else reverse("core:notifications")


def attention_summary(user, unread_count=None):
    notes = user.notifications.filter(read_at__isnull=True)
    unread_count = notes.count() if unread_count is None else unread_count
    alerts = [{"title": n.title, "body": n.body, "url": local_link(n.link),
               "level": n.level, "kind": "Recorded alert"} for n in notes[:8]]
    overdue, count = [], 0
    for key, queryset in overdue_sources(user, {}).items():
        count += queryset.count()
        for obj in queryset[:2]:
            row = row_for(key, obj)
            overdue.append({
                "title": row["title"] + " is overdue",
                "body": " · ".join(str(value) for value in (
                    row.get("kind"), row.get("provider"), row.get("due_label"),
                    row.get("next_action")) if value),
                "url": local_link(row["url"]), "level": "critical", "kind": "Live overdue work",
            })
    return {"unread_count": unread_count, "overdue_count": count,
            "total": unread_count + count, "alerts": alerts, "overdue": overdue,
            "overdue_url": reverse("core:my_work") + "?tab=overdue&mode=list"}
