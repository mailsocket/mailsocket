from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inboxes", "0004_message_html_body"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="usage_counted",
            field=models.BooleanField(default=True),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="message",
            name="usage_counted",
            field=models.BooleanField(default=False),
        ),
    ]