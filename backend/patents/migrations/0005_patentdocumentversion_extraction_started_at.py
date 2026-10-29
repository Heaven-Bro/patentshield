# Add a lease timestamp so duplicate Celery deliveries can be single-flighted
# under select_for_update while stale work can be safely reclaimed.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("patents", "0004_alter_alertdelivery_tenant_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="patentdocumentversion",
            name="extraction_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
