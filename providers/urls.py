from django.urls import path

from providers import views

app_name = "providers"

urlpatterns = [
    path("accounts/", views.accounts, name="accounts"),
    path("", views.provider_list, name="list"),
    path("new/", views.provider_form, name="create"),
    path("<int:pk>/", views.provider_detail, name="detail"),
    path("<int:pk>/edit/", views.provider_form, name="edit"),
    path("<int:pk>/bank/", views.bank_change_request, name="bank_change"),
    path("<int:pk>/bank/<int:change_pk>/", views.bank_change_decide, name="bank_decide"),
    path("accounts/new/", views.account_form, name="account_create"),
    path("accounts/<int:pk>/", views.account_detail, name="account"),
    path("accounts/<int:pk>/edit/", views.account_form, name="account_edit"),
]
