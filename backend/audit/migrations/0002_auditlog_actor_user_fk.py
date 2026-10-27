"""Replace the UUID-only audit actor with a real user foreign key.

Historical UUID values are retained in ``legacy_actor_user_uuid``. They are not
implicitly mapped to users because the former schema cannot identify which user
record, if any, a UUID represented.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="auditlog",
            old_name="actor_user_id",
            new_name="legacy_actor_user_uuid",
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="legacy_actor_user_uuid",
            field=models.UUIDField(
                blank=True,
                db_index=True,
                editable=False,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="actor_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="audit_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
