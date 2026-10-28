from django.urls import path

from settlements import views

app_name = "settlements"

urlpatterns = [
    path("", views.instruction_list, name="list"),
    path("new/", views.instruction_new, name="new"),
    path("<int:pk>/", views.instruction_detail, name="detail"),
    path("<int:pk>/pdf/", views.instruction_pdf, name="pdf"),
    path("<int:pk>/export/<str:fmt>/", views.instruction_export, name="export"),
    path("<int:pk>/<str:action>/", views.instruction_action, name="action"),
]
