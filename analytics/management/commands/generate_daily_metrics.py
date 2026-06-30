"""
analytics/management/commands/generate_daily_metrics.py

Aggregates yesterday's (or a specified date's) CallLog data into DailyMetric.

Usage
-----
# Aggregate yesterday (default — run this daily via cron)
python manage.py generate_daily_metrics

# Aggregate a specific date
python manage.py generate_daily_metrics --date 2024-06-10

# Backfill the last 30 days
python manage.py generate_daily_metrics --days 30

Cron example (runs at 00:05 every day):
    5 0 * * * /home/ubuntu/sacco_voice/venv/bin/python \
              /home/ubuntu/sacco_voice/manage.py generate_daily_metrics \
              >> /var/log/sacco/daily_metrics.log 2>&1
"""

import logging
from collections import Counter
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Aggregate CallLog data into DailyMetric for reporting."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Date to aggregate in YYYY-MM-DD format (default: yesterday)",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="Number of past days to backfill (default: 1 = yesterday only)",
        )

    def handle(self, *args, **options):
        from voice.models import CallLog
        from analytics.models import DailyMetric

        # Resolve target dates
        if options["date"]:
            try:
                target_dates = [date.fromisoformat(options["date"])]
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(
                        f"Invalid date format: {options['date']}. Use YYYY-MM-DD."
                    )
                )
                return
        else:
            yesterday = date.today() - timedelta(days=1)
            target_dates = [
                yesterday - timedelta(days=i) for i in range(options["days"])
            ]

        for target_date in target_dates:
            self._aggregate_day(target_date, CallLog, DailyMetric)

    def _aggregate_day(self, target_date, CallLog, DailyMetric):
        """Compute and persist metrics for a single calendar day."""
        self.stdout.write(f"Processing {target_date} …")

        day_qs = CallLog.objects.filter(created_at__date=target_date)
        totals = day_qs.aggregate(
            total=Count("id"),
            avg_dur=Avg("duration_seconds"),
        )

        total_calls = totals["total"] or 0
        avg_duration = totals["avg_dur"] or 0.0

        if total_calls == 0:
            self.stdout.write(
                self.style.WARNING(f"  No calls found for {target_date} — skipping.")
            )
            return

        # AI-handled = everything except speak_to_agent intent or failed status
        escalated = day_qs.filter(intent_detected="speak_to_agent").count()
        failed = day_qs.filter(status="failed").count()
        human_total = escalated + failed
        ai_handled = max(0, total_calls - human_total)
        accuracy = round((ai_handled / total_calls) * 100, 2)

        # Most common intent
        intents = list(
            day_qs.exclude(intent_detected="")
            .values_list("intent_detected", flat=True)
        )
        top_intent = Counter(intents).most_common(1)[0][0] if intents else ""

        metric, created = DailyMetric.objects.update_or_create(
            date=target_date,
            defaults={
                "total_calls": total_calls,
                "calls_handled_by_ai": ai_handled,
                "calls_escalated_to_human": human_total,
                "average_duration_seconds": round(avg_duration, 1),
                "top_intent": top_intent,
                "accuracy_percentage": accuracy,
            },
        )

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"  {action} DailyMetric for {target_date}: "
                f"{total_calls} calls, {accuracy}% accuracy, "
                f"top intent: {top_intent or 'n/a'}"
            )
        )
