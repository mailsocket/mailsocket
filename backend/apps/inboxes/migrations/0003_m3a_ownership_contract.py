import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def require_no_null_owners(apps, schema_editor):
    table = schema_editor.quote_name(apps.get_model("inboxes", "Inbox")._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")
        remaining = cursor.fetchone()[0]
    if remaining:
        raise RuntimeError(
            f"M3 ownership contract gate failed: null_owned_inboxes={remaining}"
        )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("inboxes", "0002_m3a_ownership_backfill"),
    ]

    operations = [
        migrations.RunPython(require_no_null_owners, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="inbox",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="inbox",
            index=models.Index(
                fields=["user", "deleted_at", "-created_at", "-id"],
                name="inbox_owner_timeline_idx",
            ),
        ),
    ]
