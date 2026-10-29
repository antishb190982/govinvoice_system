from django.urls import path

from core import views
from core.control import control_centre
from core.my_work import my_work
from core.my_space import my_space
from core.space_folders import folder_form, folder_remove
from core.work_schedules import schedule_action, schedule_form
from analytics.overviews import overview

app_name = "core"

urlpatterns = [
    path("my-space/", my_space, name="my_space"),
    path("my-space/folders/new/", folder_form, name="space_folder_new"),
    path("my-space/folders/<int:pk>/", folder_form, name="space_folder_edit"),
    path("my-space/folders/<int:pk>/remove/", folder_remove, name="space_folder_remove"),
    path("my-work/", my_work, name="my_work"),
    path("my-work/schedules/new/", schedule_form, name="work_schedule_new"),
    path("my-work/schedules/<int:pk>/", schedule_form, name="work_schedule"),
    path("my-work/schedules/<int:pk>/action/", schedule_action, name="work_schedule_action"),
    path("favicon.ico", views.favicon, name="favicon"),
    path("control-centre/", control_centre, name="control_centre"),
    path("insights/<slug:section>/", overview, name="section_overview"),
    path("notifications/activity/", views.activity, name="activity"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notification_read, name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
    path("audit/", views.audit_log, name="audit"),
]
