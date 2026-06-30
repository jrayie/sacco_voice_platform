"""
analytics/management/commands/generate_sample_data.py

Populates the database with realistic-looking CallLog and DailyMetric
rows so you can develop and demo the dashboard without needing live
Africa's Talking traffic.

Usage
-----
python manage.py generate_sample_data              # 30 days, ~20 calls/day
python manage.py generate_sample_data --days 7    # last 7 days only
python manage.py generate_sample_data --flush      # delete existing data first
"""

import random
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


INTENTS = [
    ("check_balance", 0.30),
    ("check_loan_balance", 0.20),
    ("apply_loan", 0.18),
    ("loan_eligibility", 0.10),
    ("repayment_schedule", 0.08),
    ("interest_rate", 0.07),
    ("speak_to_agent", 0.05),
    ("exit", 0.02),
]

LANGUAGES = [("sw", 0.60), ("en", 0.35), ("mixed", 0.05)]

KENYAN_NUMBERS = [
    "+254712{:06d}".format(random.randint(0, 999999)) for _ in range(50)
]

SAMPLE_TRANSCRIPTS = {
    "check_balance": [
        "Salio yangu ni ngapi?",
        "What is my current account balance?",
        "Niambie salio la akaunti yangu",
    ],
    "apply_loan": [
        "Nataka mkopo wa elfu hamsini",
        "I need a loan of KSh 100,000",
        "Ninaweza kupata mkopo?",
    ],
    "check_loan_balance": [
        "Mkopo wangu ulibaki ngapi?",
        "How much do I owe on my loan?",
        "Niambie deni langu",
    ],
    "interest_rate": [
        "Riba yenu ni ngapi?",
        "What is your interest rate?",
        "Mkopo una riba gani?",
    ],
    "repayment_schedule": [
        "Nililipa lini mkopo wangu?",
        "Show me my repayment schedule",
        "Ratiba ya kulipa mkopo",
    ],
    "loan_eligibility": [
        "Je, ninastahili mkopo?",
        "Am I eligible for a loan?",
        "Ninahusika na mkopo?",
    ],
    "speak_to_agent": [
        "Niongee na mtu",
        "Connect me to an agent please",
        "Nataka kuzungumza na mfanyakazi",
    ],
    "exit": ["Asante, kwaherini", "Thank you, goodbye", "Kwaheri"],
}


def weighted_choice(choices):
    """Pick a value from a list of (value, weight) tuples."""
    population, weights = zip(*choices)
    return random.choices(population, weights=weights, k=1)[0]


class Command(BaseCommand):
    help = "Generate sample CallLog and DailyMetric data for dashboard development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Number of past days to generate data for (default: 30)",
        )
        parser.add_argument(
            "--calls-per-day",
            type=int,
            default=20,
            help="Approximate number of calls per day (default: 20)",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing CallLog and DailyMetric rows first",
        )

    def handle(self, *args, **options):
        from voice.models import CallLog
        from analytics.models import DailyMetric

        if options["flush"]:
            deleted_calls, _ = CallLog.objects.all().delete()
            deleted_metrics, _ = DailyMetric.objects.all().delete()
            self.stdout.write(
                self.style.WARNING(
                    f"Flushed {deleted_calls} calls and {deleted_metrics} metrics."
                )
            )

        days = options["days"]
        cpd = options["calls_per_day"]
        today = date.today()

        total_created = 0
        for i in range(days, 0, -1):
            target_date = today - timedelta(days=i)
            # Vary call volume ±40% to make charts interesting
            num_calls = max(1, int(cpd * random.uniform(0.6, 1.4)))
            self._create_day(target_date, num_calls, CallLog)
            total_created += num_calls
            self.stdout.write(f"  {target_date}: {num_calls} calls")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nCreated {total_created} sample calls across {days} days."
            )
        )
        self.stdout.write("Running generate_daily_metrics to build aggregates …")
        from django.core.management import call_command
        call_command("generate_daily_metrics", days=days)
        self.stdout.write(self.style.SUCCESS("Done. Open /analytics/ to see the dashboard."))

    def _create_day(self, target_date, num_calls, CallLog):
        """Create num_calls CallLog rows spread across the target_date."""
        for j in range(num_calls):
            intent = weighted_choice(INTENTS)
            language = weighted_choice(LANGUAGES)
            status = "completed" if intent != "speak_to_agent" else random.choice(
                ["completed", "failed"]
            )
            transcripts = SAMPLE_TRANSCRIPTS.get(intent, ["..."])
            transcript = random.choice(transcripts)
            duration = random.randint(15, 120)
            hour = random.randint(7, 20)   # calls mostly during business hours
            minute = random.randint(0, 59)
            second = random.randint(0, 59)
            naive_dt = datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, second
            )
            created_at = timezone.make_aware(naive_dt)

            CallLog.objects.create(
                caller_number=random.choice(KENYAN_NUMBERS),
                session_id=f"sample-{target_date}-{j:04d}-{random.randint(1000,9999)}",
                status=status,
                transcript=transcript,
                language_detected=language,
                intent_detected=intent,
                response_given=f"[Sample response for {intent}]",
                duration_seconds=duration,
                created_at=created_at,
            )
