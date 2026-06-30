"""
analytics/apps.py
"""
from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "analytics"
    verbose_name = "SACCO Analytics"

    def ready(self):
        import analytics.signals  # noqa: F401  — connects post_save receiver
