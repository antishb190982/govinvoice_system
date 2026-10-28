from django.urls import path

from org import views

app_name = "org"

urlpatterns = [
    path("", views.tree, name="tree"),
    path("new/", views.unit_form, name="create"),
    path("<int:pk>/", views.unit_detail, name="detail"),
    path("<int:pk>/edit/", views.unit_form, name="edit"),
]
