"""
analytics/admin.py
"""

from django.contrib import admin
from .models import DailyMetric, WeeklyReport


@admin.register(DailyMetric)
class DailyMetricAdmin(admin.ModelAdmin):
    list_display = (
        "date", "total_calls", "calls_handled_by_ai",
        "calls_escalated_to_human", "accuracy_percentage", "top_intent",
    )
    list_filter = ("date",)
    search_fields = ("top_intent",)
    readonly_fields = ("date",)
    ordering = ("-date",)


@admin.register(WeeklyReport)
class WeeklyReportAdmin(admin.ModelAdmin):
    list_display = ("week_start", "week_end", "generated_at")
    list_filter = ("week_start",)
    readonly_fields = ("generated_at",)
    ordering = ("-week_start",)

