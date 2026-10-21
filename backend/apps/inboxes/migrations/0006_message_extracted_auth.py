from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inboxes", "0005_message_usage_counted")]

    operations = [
        migrations.AddField(
            model_name="message",
            name="otp",
            field=models.CharField(blank=True, max_length=8, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="otp_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="magic_link",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="links",
            field=models.JSONField(blank=True, default=list, null=True),
        ),
    ]