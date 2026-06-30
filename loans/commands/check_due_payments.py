"""
loans/management/commands/check_due_payments.py

Daily cron job that scans active loans for upcoming or overdue payments
and creates PaymentReminder records for each one.

Run at 05:00 Nairobi time every day:
    0 5 * * * /home/ubuntu/sacco_voice/venv/bin/python \
              /home/ubuntu/sacco_voice/manage.py check_due_payments \
              >> /var/log/sacco/check_due_payments.log 2>&1

Usage
-----
python manage.py check_due_payments
python manage.py check_due_payments --dry-run     # preview without DB writes
python manage.py check_due_payments --email-only  # skip reminder creation, just email
"""

import logging
from datetime import date, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger(__name__)

# How many days ahead to fire a pre-due reminder
PRE_DUE_DAYS = 3


class Command(BaseCommand):
    help = "Scan active loans and create PaymentReminder records for due/overdue payments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without writing to the database",
        )
        parser.add_argument(
            "--email-only",
            action="store_true",
            help="Send the manager summary email without creating reminders",
        )

    def handle(self, *args, **options):
        from loans.models import Loan, PaymentReminder

        dry_run    = options["dry_run"]
        email_only = options["email_only"]
        today      = date.today()

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes"))

        # ── Classify loans ────────────────────────────────────────────────────
        active_loans = Loan.objects.filter(status="active").select_related("member")

        overdue_loans    = []   # next_payment_date < today
        due_today_loans  = []   # next_payment_date == today
        pre_due_loans    = []   # today < next_payment_date <= today + PRE_DUE_DAYS

        for loan in active_loans:
            npd = loan.next_payment_date
            if npd < today:
                overdue_loans.append(loan)
            elif npd == today:
                due_today_loans.append(loan)
            elif today < npd <= today + timedelta(days=PRE_DUE_DAYS):
                pre_due_loans.append(loan)

        self.stdout.write(
            f"Found: {len(overdue_loans)} overdue | "
            f"{len(due_today_loans)} due today | "
            f"{len(pre_due_loans)} due within {PRE_DUE_DAYS} days"
        )

        # ── Create reminders ──────────────────────────────────────────────────
        stats = {"created": 0, "skipped": 0, "errors": 0}

        if not email_only:
            for loan, reminder_type in (
                [(l, "overdue")   for l in overdue_loans] +
                [(l, "due_today") for l in due_today_loans] +
                [(l, "pre_due")   for l in pre_due_loans]
            ):
                self._create_reminder(
                    loan, reminder_type, today, dry_run, stats
                )

        # ── Summary email ─────────────────────────────────────────────────────
        self._send_summary_email(
            today, overdue_loans, due_today_loans, pre_due_loans, stats
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {stats['created']} | "
                f"Skipped (duplicate): {stats['skipped']} | "
                f"Errors: {stats['errors']}"
            )
        )

    @transaction.atomic
    def _create_reminder(self, loan, reminder_type, today, dry_run, stats):
        """
        Create a single PaymentReminder, skipping duplicates gracefully.
        """
        from loans.models import PaymentReminder, ReminderLog

        # Prevent duplicate reminders for the same loan/type/day
        if PaymentReminder.objects.filter(
            loan=loan,
            reminder_date=today,
            reminder_type=reminder_type,
        ).exists():
            stats["skipped"] += 1
            return

        if dry_run:
            self.stdout.write(
                f"  [DRY-RUN] Would create {reminder_type} reminder for "
                f"{loan.member.full_name} (Loan #{loan.pk})"
            )
            stats["created"] += 1
            return

        try:
            reminder = PaymentReminder.objects.create(
                loan=loan,
                member=loan.member,
                reminder_date=today,
                reminder_type=reminder_type,
                status="pending",
            )
            ReminderLog.objects.create(
                reminder=reminder,
                action="call_initiated",
                notes=f"Auto-created by check_due_payments on {today}",
            )
            stats["created"] += 1
            self.stdout.write(
                f"  Created {reminder_type} reminder #{reminder.pk} "
                f"for {loan.member.full_name}"
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to create reminder for loan #%d: %s", loan.pk, exc)
            stats["errors"] += 1

    def _send_summary_email(self, today, overdue, due_today, pre_due, stats):
        """Email the SACCO manager with today's reminder summary."""
        manager_email = getattr(settings, "SACCO_MANAGER_EMAIL", None)
        if not manager_email:
            self.stdout.write(
                self.style.WARNING(
                    "SACCO_MANAGER_EMAIL not set in settings — skipping email"
                )
            )
            return

        lines = [
            f"SACCO Payment Reminder Summary — {today}",
            "=" * 50,
            "",
            f"Overdue payments:          {len(overdue)}",
            f"Due today:                 {len(due_today)}",
            f"Due within {3} days:       {len(pre_due)}",
            "",
            f"Reminders created:         {stats['created']}",
            f"Duplicates skipped:        {stats['skipped']}",
            f"Errors:                    {stats['errors']}",
            "",
        ]

        if overdue:
            lines.append("OVERDUE LOANS:")
            for loan in overdue[:20]:  # cap at 20 in the email
                lines.append(
                    f"  {loan.member.full_name} | {loan.member.phone_number} | "
                    f"KSh {loan.monthly_installment:,.0f} | "
                    f"Due: {loan.next_payment_date}"
                )
            if len(overdue) > 20:
                lines.append(f"  … and {len(overdue) - 20} more")

        body = "\n".join(lines)

        try:
            send_mail(
                subject=f"[SACCO] Payment Reminders Summary {today}",
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@sacco.co.ke"),
                recipient_list=[manager_email],
                fail_silently=False,
            )
            self.stdout.write(f"Summary email sent to {manager_email}")
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to send summary email: %s", exc)
            self.stdout.write(self.style.WARNING(f"Email failed: {exc}"))
