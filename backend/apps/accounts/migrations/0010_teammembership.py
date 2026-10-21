#  (Team Seats): additive model. Members gain NO access yet —

# team per user max (D2): `user` is a OneToOne (UNIQUE).
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.db.models.functions.datetime


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_apikey_created_by'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamMembership',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('role', models.CharField(choices=[('owner', 'Owner'), ('member', 'Member')], default='member', max_length=16)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_memberships', to='accounts.account')),
                ('invited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='team_memberships_invited', to=settings.AUTH_USER_MODEL)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='team_membership', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'indexes': [models.Index(fields=['account'], name='team_membership_account_idx')],
            },
        ),
    ]
