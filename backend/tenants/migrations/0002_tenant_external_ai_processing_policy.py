from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantExternalAIProcessingPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=False)),
                ("provider", models.CharField(default="meta", max_length=40)),
                ("model_name", models.CharField(blank=True, default="", max_length=120)),
                ("region", models.CharField(blank=True, default="", max_length=80)),
                ("policy_version", models.CharField(default="1", max_length=40)),
                ("allowed_data_categories", models.JSONField(blank=True, default=list)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approved_tenant_ai_policies", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="external_ai_policy", to="tenants.tenant")),
            ],
            options={
                "verbose_name": "tenant external AI processing policy",
                "verbose_name_plural": "tenant external AI processing policies",
            },
        ),
    ]
