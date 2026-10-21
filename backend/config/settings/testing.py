from .base import *

DEBUG = False

_db_name = get_required_env("DB_NAME")
if _db_name.startswith("test_"):
    raise ImproperlyConfigured("DB_NAME must not start with 'test_' in testing settings")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _db_name,
        "USER": get_required_env("DB_USERNAME"),
        "PASSWORD": get_required_env("DB_PASS", allow_empty=True),
        "HOST": get_required_env("DB_HOST"),
        "PORT": get_required_env("DB_PORT"),
        "TEST": {"NAME": f"test_{_db_name}"},
    }
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
# No gunicorn here (pytest / runserver): the gunicorn DB-connection budget
# (mailsocket.E001/E002) does not apply. The wait-capacity checks still run.
DB_CONNECTION_BUDGET_CHECK = False
# Tests never talk to the real Telegram Bot API, even if the shell exports a
# token: tests that need the channel set fake values via override_settings.
OPS_TELEGRAM_BOT_TOKEN = ""
OPS_TELEGRAM_CHAT_ID = ""

# Single-host test client: no apex<->dash redirects and relative links unless a
# test turns it on (settings fixture). Hard-coded, not env, so a dev's .env
# can never change test behaviour.
CANONICAL_HOST_REDIRECTS = False
