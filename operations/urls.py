from django.urls import path
from operations import views
from operations.workspace import workspace
from operations import preparation
app_name = "operations"
urlpatterns = [
    path("prepare/", preparation.prepare_new, name="prepare_new"),
    path("prepare/<uuid:pk>/", preparation.prepare, name="prepare"),
    path("my-batches/", workspace, name="workspace"),
    path("batches/", views.batches, name="batches"),
    path("batches/new/", views.batch_new, name="batch_new"),
    path("batches/<int:pk>/", views.batch, name="batch"),
    path("disputes/", views.cases, name="cases"),
    path("disputes/new/", views.case_new, name="case_new"),
    path("disputes/<int:pk>/", views.case, name="case"),
    path("disputes/<int:pk>/documents/<str:kind>/", views.document, name="document"),
]
