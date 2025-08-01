from django.apps import AppConfig


class CommunityConfig(AppConfig):
    name = "kitsune.community"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from kitsune.community import signals  # noqa
