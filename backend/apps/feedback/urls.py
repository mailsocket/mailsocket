from django.urls import path

from . import views

app_name = "feedback"

urlpatterns = [
    path("feedback/", views.public_feedback, name="public"),
    path("app/feedback/", views.app_feedback, name="app"),
]
