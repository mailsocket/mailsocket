"""Settings for the local Playwright journey suite (e2e/, scripts/e2e-local.sh).

Built on ``testing`` (DEBUG off, MD5 hasher, Telegram off, no canonical-host
redirects) with three differences:

* Auth mail goes to FILES (``EMAIL_FILE_PATH``, default ``<repo>/e2e/.mail``),
  so the signup spec reads the verification link from the newest file
  instead of a real inbox.
* Every emailed link and copy-paste snippet points at the local server
  (``E2E_BASE_URL``, default http://127.0.0.1:8099), and the dashboard-host
  split is off: one runserver serves the public pages and the dashboard.
* The product flags that are ON in production (billing, teams, custom
  domains) are on here too, so the suite walks the same pages a customer
  sees. Billing never talks to Lemon Squeezy from these pages for a Free
  user: the upgrade buttons only render, and the suite never clicks them.

Never used outside a developer machine: the management commands e2e_seed
refuses to run under any other settings module.
"""
import os

from .testing import *  
from .testing import BASE_DIR

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8099").rstrip("/")

EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = os.environ.get("EMAIL_FILE_PATH", str(BASE_DIR.parent / "e2e" / ".mail"))

PUBLIC_BASE_URL = E2E_BASE_URL
DASHBOARD_HOST = ""
APEX_HOST = ""
CANONICAL_HOST_REDIRECTS = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
CSRF_TRUSTED_ORIGINS = [E2E_BASE_URL]

ENABLE_BILLING = True
ENABLE_TEAMS = True
ENABLE_BYOD = True
