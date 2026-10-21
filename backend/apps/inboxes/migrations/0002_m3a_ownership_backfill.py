from django.db import migrations


def require_backfilled_ownership(apps, schema_editor):
    Inbox = apps.get_model("inboxes", "Inbox")
    remaining = Inbox.objects.filter(user_id__isnull=True).count()
    if remaining:
        raise RuntimeError(
            f"M3 ownership backfill gate failed: null_owned_inboxes={remaining}; "
            "run backfill_m3_accounts first"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_m3a_expand"),
        ("inboxes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(require_backfilled_ownership, migrations.RunPython.noop),
    ]
