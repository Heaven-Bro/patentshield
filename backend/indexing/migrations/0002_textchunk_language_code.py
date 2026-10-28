# Generated for Phase 5 multilingual cross-lingual retrieval.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("indexing", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="textchunk",
            name="language_code",
            field=models.CharField(db_index=True, default="und", max_length=8),
        ),
    ]
