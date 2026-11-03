from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("patents", "0011_surveillance_poll_queued_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertdelivery",
            name="active_task_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="alertdelivery",
            name="dispatch_reserved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="alertdelivery",
            index=models.Index(
                fields=["status", "dispatch_reserved_at"],
                name="surv_deliv_dispatch_idx",
            ),
        ),
    ]
