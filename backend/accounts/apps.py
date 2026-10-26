from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Register the session-authentication API module."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
