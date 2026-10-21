from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_m3a_expand"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="verification_nonce",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
