from django.urls import path

from . import views

urlpatterns = [
    path("webhook/ls/", views.ls_webhook, name="ls-webhook"),
]
