from django.urls import path

from signatures import views

app_name = "signatures"

urlpatterns = [
    path("appearance/", views.appearance, name="appearance"),
    path("", views.my_identity, name="mine"),
    path("revoke/", views.revoke_mine, name="revoke"),
    path("verify/", views.verify, name="verify"),
    path("ca.pem", views.ca_certificate, name="ca"),
]
