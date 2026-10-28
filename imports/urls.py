from django.urls import path

from imports import views

app_name = "imports"

urlpatterns = [
    path("", views.batch_list, name="list"),
    path("template.csv", views.template_csv, name="template"),
    path("<int:pk>/", views.batch_detail, name="batch"),
    path("<int:pk>/rows/<int:row_pk>/", views.batch_row, name="row"),
    path("<int:pk>/commit/", views.batch_commit, name="commit"),
    path("<int:pk>/recheck/", views.batch_recheck, name="recheck"),
    path("<int:pk>/discard/", views.batch_discard, name="discard"),
]
