from django.urls import path

from workflow import views
from workflow import configuration, monitor
from workflow.batch_approval import review

app_name = "workflow"

urlpatterns = [
    path("batches/<int:pk>/journey/", monitor.batch_map, name="batch_process"),
    path("batches/<int:pk>/", review, name="batch_review"),
    path("", views.inbox, name="inbox"),
    path("sla/", views.sla_board, name="sla"),
    path("policies/", views.policies, name="policies"),
    path("policies/new/", configuration.policy_form, name="policy_new"),
    path("policies/<int:pk>/", configuration.policy_detail, name="policy"),
    path("policies/<int:pk>/edit/", configuration.policy_form, name="policy_edit"),
    path("processes/", monitor.monitor, name="processes"),
    path("processes/<int:pk>/", monitor.invoice_map, name="process"),
    path("tasks/<int:pk>/", views.task_detail, name="task"),
]
