from django.urls import path

from ocr import views

app_name = "ocr"

urlpatterns = [
    path("documents/<int:pk>/", views.document, name="document"),
    path("documents/<int:pk>/reanalyse/", views.reanalyse, name="reanalyse"),
    path("documents/<int:pk>/pages/<int:page>.png", views.preview, name="preview"),
    path("templates/", views.template_list, name="templates"),
    path("templates/<int:provider_pk>/", views.template_edit, name="template_edit"),
]
