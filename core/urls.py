from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notification_read, name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
    path("audit/", views.audit_log, name="audit"),
]
