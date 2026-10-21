from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register_view, name="register"),
    path("verify-email/resend/", views.resend_verification, name="resend-verification"),
    path("verify-email/<str:token>/", views.verify_email, name="verify-email"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password-reset/", views.password_reset_request, name="password-reset"),
    path(
        "password-reset/confirm/<str:uidb64>/<str:token>/",
        views.password_reset_confirm,
        name="password-reset-confirm",
    ),
]
