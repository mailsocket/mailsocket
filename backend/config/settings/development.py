import os

from .base import *

DEBUG = os.environ.get("DEBUG", "true").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,web"
    ).split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
# No gunicorn here (pytest / runserver): the gunicorn DB-connection budget
# (mailsocket.E001/E002) does not apply. The wait-capacity checks still run.
DB_CONNECTION_BUDGET_CHECK = False
