"""PROD-INCIDENT regression test (2026-09-28).

config/settings/production.py used to require OUTBOUND_DKIM_MASTER_KEY
unconditionally, even though it is an OUTBOUND-ONLY feature secret consumed
only by apps/outbound/dkim_keys.py when ENABLE_OUTBOUND=True. Prod had
ENABLE_OUTBOUND=False (the default) but the var was absent from prod .env,
so Django settings failed to import at all -> web crash-looped -> the
deploy's migrate step was killed.

These tests import config.settings.production directly (via importlib, with
a controlled os.environ and a fresh module reload each time) to prove:

1. ENABLE_OUTBOUND=false + OUTBOUND_DKIM_MASTER_KEY absent -> settings import
   succeeds (this is the exact regression scenario that took prod down).
2. ENABLE_OUTBOUND=true + the var absent -> ImproperlyConfigured is raised
   (the gate still protects the real outbound code path).
3. ENABLE_OUTBOUND=true + the var present -> settings import succeeds and
   OUTBOUND_DKIM_MASTER_KEY equals the provided value.
"""

import importlib
import os
import sys

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

# Minimal set of env vars production.py requires unconditionally (core infra:
# SECRET_KEY, ALLOWED_HOSTS, INGEST_SHARED_SECRET, AUTH_RATE_LIMIT_SECRET,
# API_KEY_PEPPER, WEBHOOK_SIGNING_MASTER_KEY, DB_*). These must stay present
# in every case below so the only variable under test is the outbound gate.
BASE_REQUIRED_ENV = {
    "SECRET_KEY": "test-secret-key",
    "ALLOWED_HOSTS": "example.com",
    "INGEST_SHARED_SECRET": "test-ingest-shared-secret",
    "AUTH_RATE_LIMIT_SECRET": "test-auth-rate-limit-secret",
    "API_KEY_PEPPER": "test-api-key-pepper",
    "WEBHOOK_SIGNING_MASTER_KEY": "test-webhook-signing-master-key",
    "DB_NAME": "test_db",
    "DB_USERNAME": "test_user",
    "DB_PASS": "test_pass",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
}

PRODUCTION_MODULE = "config.settings.production"
BASE_MODULE = "config.settings.base"


def _import_production_with_env(extra_env):
    """Reload config.settings.production under a controlled environment.

    Removes any cached config.settings.{base,production} modules first so
    each call is a clean import (production.py does `from .base import *`
    at module level, so base must be reloaded too or stale state leaks
    between test cases).
    """
    env = dict(BASE_REQUIRED_ENV)
    env.update(extra_env)

    old_environ = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    for mod_name in (PRODUCTION_MODULE, BASE_MODULE):
        sys.modules.pop(mod_name, None)
    try:
        return importlib.import_module(PRODUCTION_MODULE)
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        for mod_name in (PRODUCTION_MODULE, BASE_MODULE):
            sys.modules.pop(mod_name, None)


class OutboundDkimMasterKeyGateTests(SimpleTestCase):
    def test_enable_outbound_false_and_key_absent_imports_cleanly(self):
        """The exact regression: flag OFF, secret absent -> must NOT crash."""
        settings_module = _import_production_with_env(
            {"ENABLE_OUTBOUND": "false"}
        )
        self.assertFalse(settings_module.ENABLE_OUTBOUND)
        # Falls back to base.py's dev-only default; never consumed while off.
        self.assertEqual(
            settings_module.OUTBOUND_DKIM_MASTER_KEY,
            "development-only-outbound-dkim-master-key",
        )

    def test_enable_outbound_true_and_key_absent_raises(self):
        """The gate must still protect the real outbound code path."""
        with self.assertRaises(ImproperlyConfigured):
            _import_production_with_env({"ENABLE_OUTBOUND": "true"})

    def test_enable_outbound_true_and_key_present_loads_value(self):
        settings_module = _import_production_with_env(
            {
                "ENABLE_OUTBOUND": "true",
                "OUTBOUND_DKIM_MASTER_KEY": "provided-master-key-value",
            }
        )
        self.assertTrue(settings_module.ENABLE_OUTBOUND)
        self.assertEqual(
            settings_module.OUTBOUND_DKIM_MASTER_KEY, "provided-master-key-value"
        )
