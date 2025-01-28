# Generated by Django 4.2.18 on 2025-01-23 08:29

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("flagit", "0004_alter_flaggedobject_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="flaggedobject",
            name="assigned_timestamp",
            field=models.DateTimeField(default=None, null=True),
        ),
        migrations.AddField(
            model_name="flaggedobject",
            name="assignee",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_flags",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]