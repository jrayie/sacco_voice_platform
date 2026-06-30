"""
analytics/management/commands/generate_weekly_report.py

Computes a WeeklyReport by aggregating DailyMetric rows for an ISO week.

Usage
-----
# Generate report for the most recently completed ISO week (default)
python manage.py generate_weekly_report

# Generate for a specific week containing this date
python manage.py generate_weekly_report --date 2024-06-10

# Backfill the last N weeks
python manage.py generate_weekly_report --weeks 4

Cron (runs Monday at 00:15, covers the week just ended):
    15 0 * * 1  /home/ubuntu/sacco_voice/venv/bin/python \\
                /home/ubuntu/sacco_voice/manage.py generate_weekly_report \\
                >> /var/log/sacco/weekly_report.log 2>&1
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg, Sum


class Command(BaseCommand):
    help = "Aggregate DailyMetric rows into WeeklyReport for a given ISO week."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help=(
                "Any date (YYYY-MM-DD) within the target week. "
                "Defaults to last completed week."
            ),
        )
        parser.add_argument(
            "--weeks",
            type=int,
            default=1,
            help="Number of past weeks to generate (default: 1).",
        )

    def handle(self, *args, **options):
        from analytics.models import DailyMetric, WeeklyReport

        if options["date"]:
            try:
                anchor = date.fromisoformat(options["date"])
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(f"Invalid date: {options['date']}. Use YYYY-MM-DD.")
                )
                return
            # Snap to Monday of that week
            anchors = [anchor - timedelta(days=anchor.weekday())]
        else:
            # Most recently completed Monday
            today = date.today()
            last_monday = today - timedelta(days=today.weekday() + 7)
            anchors = [
                last_monday - timedelta(weeks=i)
                for i in range(options["weeks"])
            ]

        for week_start in anchors:
            week_end = week_start + timedelta(days=6)
            self._generate(week_start, week_end, DailyMetric, WeeklyReport)

    def _generate(self, week_start, week_end, DailyMetric, WeeklyReport):
        self.stdout.write(f"Generating report for {week_start} → {week_end} …")

        day_metrics = DailyMetric.objects.filter(date__range=(week_start, week_end))

        if not day_metrics.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"  No DailyMetric rows for {week_start}–{week_end}. "
                    "Run generate_daily_metrics --days 7 first."
                )
            )
            return

        agg = day_metrics.aggregate(
            total=Sum("total_calls"),
            ai=Sum("calls_handled_by_ai"),
            escalated=Sum("calls_escalated_to_human"),
            avg_dur=Avg("average_duration_seconds"),
        )
        total = agg["total"] or 0
        ai    = agg["ai"] or 0
        esc   = agg["escalated"] or 0
        accuracy = round(ai / total * 100, 2) if total else 0.0

        # Intent totals across the week — pull from CallLog directly
        from voice.models import CallLog
        from django.db.models import Count

        top_intents = (
            CallLog.objects.filter(created_at__date__range=(week_start, week_end))
            .exclude(intent_detected="")
            .values("intent_detected")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:10]
        )

        daily_breakdown = [
            {
                "date": str(m.date),
                "calls": m.total_calls,
                "accuracy": m.accuracy_percentage,
            }
            for m in day_metrics.order_by("date")
        ]

        report_data = {
            "total_calls": total,
            "ai_handled": ai,
            "escalated": esc,
            "accuracy": accuracy,
            "avg_duration": round(agg["avg_dur"] or 0, 1),
            "top_intents": [
                {"intent": r["intent_detected"], "count": r["cnt"]}
                for r in top_intents
            ],
            "daily_breakdown": daily_breakdown,
        }

        report, created = WeeklyReport.objects.update_or_create(
            week_start=week_start,
            week_end=week_end,
            defaults={"report_data": report_data},
        )
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"  {action} WeeklyReport: {total} calls, {accuracy}% accuracy"
            )
        )
