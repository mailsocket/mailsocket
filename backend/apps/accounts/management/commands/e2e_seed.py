"""Local e2e seed: prepares a disposable DB for the Playwright journey suite.

Creates nothing permanent. It only:
  * empties the file-backend mail directory (``EMAIL_FILE_PATH``) so the
    signup spec's "newest mail file" is this run's mail;
  * clears every RateLimitBucket row, so a re-run on the same local DB is not
    throttled by the previous run's signups / test codes / key mints.

Refuses to run unless the settings module is ``config.settings.testing`` or
``config.settings.e2e`` (never production/development), refuses a DB_NAME
that does not look like a local e2e/test database, and refuses to empty an
``EMAIL_FILE_PATH`` whose resolved path does not end in ``e2e/.mail`` (a
mis-set env var such as ``$HOME`` must never be wiped).
"""
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import RateLimitBucket

ALLOWED_SETTINGS = {"config.settings.testing", "config.settings.e2e"}
MAIL_DIR_SUFFIX = ("e2e", ".mail")


def assert_safe_mail_dir(mail_dir: str) -> Path:
    """Raise CommandError unless the resolved path ends with ``e2e/.mail``."""
    path = Path(mail_dir).expanduser().resolve()
    if path.parts[-2:] != MAIL_DIR_SUFFIX:
        raise CommandError(
            f"refusing to empty EMAIL_FILE_PATH {str(path)!r}: it must resolve to a directory ending in e2e/.mail"
        )
    return path


def assert_e2e_settings() -> str:
    """Raise CommandError unless we run under the testing/e2e settings module."""
    module = os.environ.get("DJANGO_SETTINGS_MODULE", "") or getattr(settings, "SETTINGS_MODULE", "")
    if module not in ALLOWED_SETTINGS:
        raise CommandError(
            f"refusing to run under settings {module!r}: only {sorted(ALLOWED_SETTINGS)} are allowed"
        )
    return module


class Command(BaseCommand):
    help = "Prepare the local e2e DB (testing/e2e settings only). Creates nothing permanent."

    def handle(self, *args, **options):
        module = assert_e2e_settings()
        db_name = settings.DATABASES["default"]["NAME"]
        if not any(tag in db_name for tag in ("e2e", "test")):
            raise CommandError(f"refusing DB {db_name!r}: name must contain 'e2e' or 'test'")

        mail_dir = getattr(settings, "EMAIL_FILE_PATH", "")
        removed = 0
        if mail_dir:
            path = assert_safe_mail_dir(mail_dir)
            if path.is_dir():
                for child in path.iterdir():
                    if child.is_file():
                        child.unlink()
                        removed += 1
                    elif child.is_dir():
                        shutil.rmtree(child)
            path.mkdir(parents=True, exist_ok=True)

        buckets, _ = RateLimitBucket.objects.all().delete()
        self.stdout.write(
            f"e2e_seed: settings={module} db={db_name} mail_dir={mail_dir or '-'} "
            f"mail_files_removed={removed} rate_limit_buckets_cleared={buckets}"
        )
