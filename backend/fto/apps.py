from django.apps import AppConfig


class FTOConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fto"
    verbose_name = "FTO and Product-to-Claim Mapping"
