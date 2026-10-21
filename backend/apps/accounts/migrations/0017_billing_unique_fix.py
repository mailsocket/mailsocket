

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0016_w10_account_ownership'),
    ]

    operations = [
        migrations.AlterField(
            model_name='accountplan',
            name='stripe_customer_id',
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='accountplan',
            name='stripe_price_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='accountplan',
            name='billing_synced_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
