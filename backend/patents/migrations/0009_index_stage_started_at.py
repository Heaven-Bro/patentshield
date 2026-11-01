from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("patents", "0008_index_stage_task_ids")]

    operations = [
        migrations.AddField(
            model_name="patentdocumentversion",
            name="text_indexing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="schematic_indexing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
