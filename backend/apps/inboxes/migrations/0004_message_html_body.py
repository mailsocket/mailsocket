from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inboxes", "0003_m3a_ownership_contract")]

    operations = [
        migrations.AddField(
            model_name="message",
            name="html_body",
            field=models.TextField(blank=True),
        ),
    ]