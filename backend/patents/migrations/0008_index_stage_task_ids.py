from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("patents", "0007_stage_status_embedding_identity_and_public_hnsw")]

    operations = [
        migrations.AddField(
            model_name="patentdocumentversion",
            name="text_indexing_task_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="schematic_indexing_task_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
