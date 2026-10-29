from django.urls import path

from imports import views
from imports import registration

app_name = "imports"

urlpatterns = [
    path("", views.batch_list, name="list"),
    path("registration/", registration.registration_hub, name="registration"),
    path("registration/requests/", registration.registration_queue, name="registration_queue"),
    path("registration/requests/<int:pk>/", registration.registration_request, name="registration_request"),
    path("registration/requests/<int:pk>/sources/<int:row_pk>/",
         registration.registration_source, name="registration_source"),
    path("template.csv", views.template_csv, name="template"),
    path("<int:pk>/", views.batch_detail, name="batch"),
    path("<int:pk>/rows/<int:row_pk>/", views.batch_row, name="row"),
    path("<int:pk>/commit/", views.batch_commit, name="commit"),
    path("<int:pk>/recheck/", views.batch_recheck, name="recheck"),
    path("<int:pk>/analyse/", views.batch_analyse, name="analyse"),
    path("<int:pk>/discard/", views.batch_discard, name="discard"),
]
