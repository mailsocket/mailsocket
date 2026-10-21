from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_delete

        # Registers the fail-closed runtime sizing invariants (mailsocket.E00x).
        from . import checks  
        from .signals import revoke_keys_minted_by_deleted_user

        pre_delete.connect(
            revoke_keys_minted_by_deleted_user,
            sender=get_user_model(),
            dispatch_uid="accounts.revoke_keys_minted_by_deleted_user",
        )
