

import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BillingEvent',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('event_key', models.CharField(max_length=80, unique=True)),
                ('event_name', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='billing_events', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
