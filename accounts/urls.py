from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("2fa/verify/", views.otp_verify, name="otp_verify"),
    path("2fa/enrol/", views.otp_enroll, name="otp_enroll"),
    path("2fa/recovery-codes/", views.recovery_codes, name="recovery_codes"),
    path("password/", views.password_change, name="password_change"),
    path("profile/", views.profile, name="profile"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_detail, name="user_detail"),
]
