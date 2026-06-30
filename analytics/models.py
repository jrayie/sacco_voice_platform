"""
analytics/models.py

Stores pre-aggregated metrics so the dashboard loads fast even at scale.
Raw call data lives in voice.CallLog; these models are derived summaries.

Models
------
DailyMetric   — one row per calendar day, computed by generate_daily_metrics
WeeklyReport  — one row per ISO week, computed on demand or via cron
"""

from django.db import models


class DailyMetric(models.Model):
    """
    Aggregated KPIs for a single calendar day.

    Populated by the generate_daily_metrics management command (or the
    analytics.signals module for real-time updates when a CallLog is saved).
    """

    date = models.DateField(unique=True, db_index=True)

    total_calls = models.IntegerField(default=0)
    calls_handled_by_ai = models.IntegerField(
        default=0,
        help_text="Calls where AI resolved the query (intent != speak_to_agent)",
    )
    calls_escalated_to_human = models.IntegerField(
        default=0,
        help_text="Calls where intent == speak_to_agent or status == failed",
    )
    average_duration_seconds = models.FloatField(
        default=0.0,
        help_text="Mean call duration in seconds for the day",
    )
    top_intent = models.CharField(
        max_length=100,
        blank=True,
        help_text="Most frequently detected intent for the day",
    )
    accuracy_percentage = models.FloatField(
        default=0.0,
        help_text="(ai_handled / total_calls) * 100",
    )

    class Meta:
        ordering = ["-date"]
        verbose_name = "Daily Metric"
        verbose_name_plural = "Daily Metrics"

    def __str__(self):
        return (
            f"{self.date} | {self.total_calls} calls | "
            f"{self.accuracy_percentage:.1f}% accuracy"
        )


class WeeklyReport(models.Model):
    """
    Weekly summary derived from DailyMetric rows.

    report_data JSON shape:
    {
        "total_calls": 312,
        "ai_handled": 278,
        "escalated": 34,
        "accuracy": 89.1,
        "avg_duration": 47.3,
        "top_intents": [
            {"intent": "check_balance", "count": 120},
            {"intent": "apply_loan",    "count": 85}
        ],
        "daily_breakdown": [
            {"date": "2024-06-10", "calls": 41, "accuracy": 88.0}
        ]
    }
    """

    week_start = models.DateField(db_index=True)
    week_end = models.DateField()
    report_data = models.JSONField(default=dict)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-week_start"]
        unique_together = [("week_start", "week_end")]
        verbose_name = "Weekly Report"
        verbose_name_plural = "Weekly Reports"

    def __str__(self):
        return f"Week {self.week_start} → {self.week_end}"
