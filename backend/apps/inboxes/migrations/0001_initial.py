import django.core.validators
import django.db.models.deletion
import django.db.models.functions.datetime
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Inbox',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('public_id', models.CharField(editable=False, max_length=32, unique=True)),
                ('label', models.CharField(blank=True, max_length=120)),
                ('routing_key', models.CharField(max_length=64, unique=True, validators=[django.core.validators.RegexValidator('^[a-z0-9][a-z0-9_-]{2,63}$')])),
                ('is_enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='Message',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('public_id', models.CharField(editable=False, max_length=32, unique=True)),
                ('dedupe_key', models.CharField(max_length=64)),
                ('provider_message_id', models.CharField(blank=True, db_index=True, max_length=998, null=True)),
                ('postfix_queue_id', models.CharField(blank=True, db_index=True, max_length=64, null=True)),
                ('intake_id', models.CharField(blank=True, max_length=128, null=True)),
                ('envelope_from', models.CharField(blank=True, max_length=320)),
                ('envelope_recipient', models.CharField(max_length=320)),
                ('header_from', models.CharField(blank=True, max_length=998)),
                ('header_to', models.JSONField(default=list)),
                ('header_cc', models.JSONField(default=list)),
                ('subject', models.CharField(blank=True, max_length=998)),
                ('text_body', models.TextField(blank=True)),
                ('detected_otp', models.CharField(blank=True, max_length=8, null=True)),
                ('raw_bytes', models.BinaryField()),
                ('raw_sha256', models.CharField(db_index=True, max_length=64)),
                ('size_bytes', models.BigIntegerField()),
                ('ingest_status', models.CharField(choices=[('received', 'Received'), ('parsing', 'Parsing'), ('parsed', 'Parsed'), ('failed', 'Failed')], default='received', max_length=16)),
                ('parse_attempts', models.IntegerField(default=0)),
                ('parse_error', models.CharField(blank=True, max_length=1000)),
                ('parse_started_at', models.DateTimeField(blank=True, null=True)),
                ('parse_lease_token', models.UUIDField(blank=True, null=True)),
                ('parse_lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('next_parse_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('parsed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('inbox', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='messages', to='inboxes.inbox')),
            ],
        ),
        migrations.AddIndex(
            model_name='inbox',
            index=models.Index(condition=models.Q(('deleted_at__isnull', True), ('is_enabled', True)), fields=['routing_key'], name='inbox_active_route_idx'),
        ),
        migrations.AddConstraint(
            model_name='inbox',
            constraint=models.CheckConstraint(condition=models.Q(('routing_key', django.db.models.functions.text.Lower('routing_key'))), name='inbox_routing_key_lower'),
        ),
        migrations.AddConstraint(
            model_name='inbox',
            constraint=models.CheckConstraint(condition=models.Q(('routing_key__regex', '^[a-z0-9][a-z0-9_-]{2,63}$')), name='inbox_routing_key_format'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['inbox', '-received_at', '-id'], name='message_inbox_timeline_idx'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['ingest_status', 'next_parse_at', 'received_at', 'id'], name='message_reconcile_idx'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(fields=('inbox', 'dedupe_key'), name='message_inbox_dedupe_uniq'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('size_bytes__gte', 0)), name='message_size_nonnegative'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('parse_attempts__gte', 0)), name='message_attempts_nonnegative'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('raw_sha256__regex', '^[0-9a-f]{64}$')), name='message_raw_sha256_format'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('dedupe_key__regex', '^[0-9a-f]{64}$')), name='message_dedupe_key_format'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('ingest_status__in', ('received', 'parsing', 'parsed', 'failed'))), name='message_ingest_status_valid'),
        ),
    ]
