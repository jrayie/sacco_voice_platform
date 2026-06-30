"""
analytics/signals.py

Recomputes DailyMetric in real-time whenever a voice.CallLog is saved.
Connected in analytics/apps.py → AnalyticsConfig.ready().
"""

import logging
from collections import Counter

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db.models import Avg, Count

logger = logging.getLogger(__name__)


def _get_models():
    from voice.models import CallLog
    from analytics.models import DailyMetric
    return CallLog, DailyMetric


@receiver(post_save)
def on_call_log_saved(sender, instance, **kwargs):
    """Fires after any model save; filters to voice.CallLog only."""
    CallLog, DailyMetric = _get_models()
    if not isinstance(instance, CallLog):
        return
    if instance.status not in ("completed", "failed"):
        return
    try:
        _recompute_day(instance.created_at.date())
    except Exception as exc:          # pylint: disable=broad-except
        logger.error("analytics signal error: %s", exc, exc_info=True)


def _recompute_day(date):
    """Aggregate a single day's CallLog rows into DailyMetric."""
    CallLog, DailyMetric = _get_models()

    qs = CallLog.objects.filter(created_at__date=date)
    agg = qs.aggregate(total=Count("id"), avg_dur=Avg("duration_seconds"))

    total = agg["total"] or 0
    avg_dur = agg["avg_dur"] or 0.0
    escalated = qs.filter(intent_detected="speak_to_agent").count()
    failed = qs.filter(status="failed").count()
    human_total = escalated + failed
    ai_handled = max(0, total - human_total)
    accuracy = (ai_handled / total * 100) if total else 0.0

    intents = list(qs.exclude(intent_detected="").values_list("intent_detected", flat=True))
    top = Counter(intents).most_common(1)[0][0] if intents else ""

    DailyMetric.objects.update_or_create(
        date=date,
        defaults={
            "total_calls": total,
            "calls_handled_by_ai": ai_handled,
            "calls_escalated_to_human": human_total,
            "average_duration_seconds": round(avg_dur, 1),
            "top_intent": top,
            "accuracy_percentage": round(accuracy, 2),
        },
    )
    logger.info("Recomputed DailyMetric for %s (%d calls)", date, total)
