#  (Team Seats): additive model. token_hash stores a sha256
# hex digest ONLY (secrets.token_urlsafe(32) raw token is never persisted —
# same discipline as ApiKey.key_hash). Partial unique enforces one live
# invite per (account, email) at a time; a new invite can be issued again
# once the prior one is accepted/revoked.
import apps.accounts.models
import django.core.validators
import django.db.models.deletion
import django.db.models.functions.datetime
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_teammembership'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamInvite',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('email_normalized', models.CharField(max_length=254, validators=[django.core.validators.EmailValidator()])),
                ('token_hash', models.CharField(editable=False, max_length=64, unique=True)),
                ('role', models.CharField(choices=[('owner', 'Owner'), ('member', 'Member')], default='member', max_length=16)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('expires_at', models.DateTimeField(default=apps.accounts.models._default_invite_expiry, editable=False)),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_invites', to='accounts.account')),
                ('invited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='team_invites_sent', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'indexes': [models.Index(fields=['account'], name='team_invite_account_idx')],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('email_normalized', django.db.models.functions.text.Lower('email_normalized'))), name='team_invite_email_normalized_lower'),
                    models.UniqueConstraint(condition=models.Q(('accepted_at__isnull', True), ('revoked_at__isnull', True)), fields=('account', 'email_normalized'), name='team_invite_live_account_email_uniq'),
                ],
            },
        ),
    ]
