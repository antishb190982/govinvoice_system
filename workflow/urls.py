from django.urls import path

from workflow import views

app_name = "workflow"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("sla/", views.sla_board, name="sla"),
    path("policies/", views.policies, name="policies"),
    path("tasks/<int:pk>/", views.task_detail, name="task"),
]
