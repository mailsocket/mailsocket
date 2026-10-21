"Public API."
from django.utils import timezone


def revoke_keys_minted_by_deleted_user(sender, instance, **kwargs):
    from .models import ApiKey

    ApiKey.objects.filter(created_by_id=instance.pk, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
