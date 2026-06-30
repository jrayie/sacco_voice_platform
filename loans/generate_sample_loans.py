"""
loans/management/commands/generate_sample_loans.py

Generates realistic sample data for local development and dashboard demos.

Creates:
  - 50 Members (with MemberPreference rows)
  - 100 active Loans across various due-date scenarios
  - PaymentReminder and ReminderLog history for the past 14 days

Usage
-----
python manage.py generate_sample_loans
python manage.py generate_sample_loans --flush   # wipe existing loan data first
python manage.py generate_sample_loans --members 20 --loans 40
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone


# Realistic Kenyan first/last names for sample data
FIRST_NAMES_SW = [
    "Amina", "Baraka", "Zawadi", "Furaha", "Neema", "Rehema", "Hamisi",
    "Juma", "Kadzo", "Mwangi", "Njeri", "Otieno", "Wanjiku", "Kamau",
    "Akinyi", "Omondi", "Adhiambo", "Kipchoge", "Chebet", "Mutua",
]
LAST_NAMES = [
    "Wanjiku", "Odhiambo", "Kamau", "Mutua", "Otieno", "Njoroge",
    "Mwangi", "Kipkoech", "Auma", "Gitonga", "Wambua", "Kariuki",
    "Ndung'u", "Mugo", "Nyambura", "Kiprotich", "Korir", "Cheruiyot",
    "Langat", "Ruto",
]

PRINCIPAL_AMOUNTS = [
    50_000, 75_000, 100_000, 150_000, 200_000,
    250_000, 300_000, 500_000, 750_000, 1_000_000,
]


def random_phone() -> str:
    """Generate a random Kenyan mobile number in E.164 format."""
    prefix = random.choice(["0712", "0722", "0733", "0743", "0768", "0798"])
    suffix = "".join(str(random.randint(0, 9)) for _ in range(6))
    number = "+254" + prefix[1:] + suffix
    return number


def monthly_installment(principal: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    """Calculate flat-rate monthly installment (simplified for demo)."""
    interest_total = principal * annual_rate / 100
    return (principal + interest_total) / months


class Command(BaseCommand):
    help = "Generate sample Members, Loans, and Reminders for dashboard development."

    def add_arguments(self, parser):
        parser.add_argument("--members", type=int, default=50)
        parser.add_argument("--loans",   type=int, default=100)
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing loan data before generating",
        )

    def handle(self, *args, **options):
        from loans.models import (
            Member, MemberPreference, Loan, PaymentReminder, ReminderLog
        )

        if options["flush"]:
            counts = {
                "ReminderLog":      ReminderLog.objects.all().delete()[0],
                "PaymentReminder":  PaymentReminder.objects.all().delete()[0],
                "Loan":             Loan.objects.all().delete()[0],
                "MemberPreference": MemberPreference.objects.all().delete()[0],
                "Member":           Member.objects.all().delete()[0],
            }
            for model, n in counts.items():
                self.stdout.write(f"  Deleted {n} {model} rows")

        # ── Members ───────────────────────────────────────────────────────────
        self.stdout.write(f"Creating {options['members']} members …")
        members = []
        existing_phones = set(Member.objects.values_list("phone_number", flat=True))

        for _ in range(options["members"]):
            phone = random_phone()
            while phone in existing_phones:
                phone = random_phone()
            existing_phones.add(phone)

            member = Member.objects.create(
                phone_number=phone,
                first_name=random.choice(FIRST_NAMES_SW),
                last_name=random.choice(LAST_NAMES),
                preferred_language=random.choices(["sw", "en"], weights=[60, 40])[0],
                is_active=True,
            )
            MemberPreference.objects.create(
                member=member,
                do_not_disturb_start="22:00",
                do_not_disturb_end="06:00",
                max_calls_per_day=random.choice([2, 3]),
                opt_out=False,
            )
            members.append(member)
        self.stdout.write(self.style.SUCCESS(f"  ✓ {len(members)} members created"))

        # ── Loans ─────────────────────────────────────────────────────────────
        self.stdout.write(f"Creating {options['loans']} loans …")
        today = date.today()

        # Spread due dates realistically across scenarios
        due_date_scenarios = (
            [today - timedelta(days=d) for d in range(1, 8)]   # 7 overdue
            + [today]                                            # 1 due today
            + [today + timedelta(days=d) for d in range(1, 4)] # 3 pre-due
            + [today + timedelta(days=d) for d in range(7, 28)] # future
        )

        loans = []
        for i in range(options["loans"]):
            member    = random.choice(members)
            principal = Decimal(random.choice(PRINCIPAL_AMOUNTS))
            rate      = Decimal(random.choice([10, 12, 14, 15]))
            term      = random.choice([12, 24, 36])
            install   = monthly_installment(principal, rate, term).quantize(Decimal("0.01"))
            # Outstanding is a random fraction of principal
            outstanding = (principal * Decimal(random.uniform(0.1, 0.95))).quantize(
                Decimal("0.01")
            )
            due_date  = random.choice(due_date_scenarios)
            status    = "active"
            if due_date < today - timedelta(days=30):
                status = random.choices(["active", "defaulted"], weights=[70, 30])[0]

            loan = Loan.objects.create(
                member=member,
                principal_amount=principal,
                outstanding_balance=outstanding,
                monthly_installment=install,
                next_payment_date=due_date,
                due_day_of_month=due_date.day,
                status=status,
                interest_rate_annual=rate,
            )
            loans.append(loan)

        self.stdout.write(self.style.SUCCESS(f"  ✓ {len(loans)} loans created"))

        # ── Historical reminders (last 14 days) ───────────────────────────────
        self.stdout.write("Generating reminder history (last 14 days) …")
        reminder_count = 0
        for loan in random.sample(loans, min(60, len(loans))):
            for days_ago in random.sample(range(1, 15), k=random.randint(1, 3)):
                r_date = today - timedelta(days=days_ago)
                r_type = random.choices(
                    ["pre_due", "due_today", "overdue"], weights=[50, 30, 20]
                )[0]
                status = random.choices(
                    ["completed", "failed", "completed"],
                    weights=[65, 15, 20],
                )[0]
                confirmed = random.random() < 0.7 if status == "completed" else False

                try:
                    reminder = PaymentReminder.objects.create(
                        loan=loan,
                        member=loan.member,
                        reminder_date=r_date,
                        reminder_type=r_type,
                        status=status,
                        call_sid=f"sample-{loan.pk}-{days_ago}",
                        response_received=(
                            "Nitalipa kesho" if confirmed else
                            "Sitaweza kulipa sasa hivi"
                        ),
                        payment_confirmed=confirmed,
                        attempts=1,
                        completed_at=timezone.now() - timedelta(days=days_ago),
                    )
                    ReminderLog.objects.create(
                        reminder=reminder,
                        action="call_initiated",
                        notes="Sample data",
                    )
                    if status == "completed":
                        ReminderLog.objects.create(
                            reminder=reminder,
                            action="payment_confirmed" if confirmed else "call_completed",
                            notes="Sample data",
                        )
                    reminder_count += 1
                except Exception:  # duplicate unique_together — skip
                    pass

        self.stdout.write(self.style.SUCCESS(f"  ✓ {reminder_count} historical reminders"))
        self.stdout.write(
            self.style.SUCCESS(
                "\nSample data ready. Visit /analytics/ or /loans/reminders/ "
                "to see the data."
            )
        )
