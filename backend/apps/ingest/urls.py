from django.urls import path

from .views import ingest

urlpatterns = [path("", ingest, name="internal-ingest")]
