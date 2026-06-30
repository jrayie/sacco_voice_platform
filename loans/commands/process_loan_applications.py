"""
loans/management/commands/process_loan_applications.py

Processes pending loan applications:
  - Generates RepaymentPlan for newly-approved applications that lack one
  - Re-runs eligibility on stale pending_review applications
  - Sends manager notification summary email

Usage
-----
python manage.py process_loan_applications
python manage.py process_loan_applications --dry-run
python manage.py process_loan_applications --app-id 42   # process one

Cron (run every hour during business hours):
    0 7-18 * * 1-5  /home/ubuntu/sacco_voice/venv/bin/python \
                    /home/ubuntu/sacco_voice/manage.py process_loan_applications \
                    >> /var/log/sacco/loan_applications.log 2>&1
"""

import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process pending loan applications: generate repayment plans and notify manager."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--app-id",  type=int, default=None,
                            help="Process a single application by PK")

    def handle(self, *args, **options):
        from loans.models import LoanApplication, RepaymentPlan
        from loans.voice_application import (
            check_eligibility, _calculate_installment, DEFAULT_INTEREST_RATE
        )

        dry_run = options["dry_run"]
        app_id  = options["app_id"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved"))

        # ── Select applications to process ────────────────────────────────────
        if app_id:
            qs = LoanApplication.objects.filter(pk=app_id)
        else:
            qs = LoanApplication.objects.filter(
                status__in=["pending_review", "approved"]
            ).select_related("member").order_by("application_date")

        stats = {"processed": 0, "approved": 0, "pending": 0, "errors": 0}

        for app in qs:
            try:
                self._process_one(app, dry_run, stats)
            except Exception as exc:   # pylint: disable=broad-except
                logger.error("Error processing app #%d: %s", app.pk, exc, exc_info=True)
                stats["errors"] += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Processed: {stats['processed']} | "
                f"Approved: {stats['approved']} | "
                f"Pending: {stats['pending']} | "
                f"Errors: {stats['errors']}"
            )
        )

        if not dry_run:
            self._send_summary(stats, qs.count())

    @transaction.atomic
    def _process_one(self, app, dry_run: bool, stats: dict):
        from loans.models import RepaymentPlan
        from loans.voice_application import (
            check_eligibility, _calculate_installment, DEFAULT_INTEREST_RATE
        )

        stats["processed"] += 1

        # ── Approved but missing repayment plan ───────────────────────────────
        if app.status == "approved":
            if not hasattr(app, "repayment_plan") or not RepaymentPlan.objects.filter(
                loan_application=app
            ).exists():
                self.stdout.write(
                    f"  Generating repayment plan for App #{app.pk} …"
                )
                if not dry_run:
                    self._generate_plan(app)
            else:
                self.stdout.write(f"  App #{app.pk} already has a repayment plan — skip")
            stats["approved"] += 1
            return

        # ── Pending review — re-run eligibility ───────────────────────────────
        if app.status == "pending_review":
            is_eligible, reason, max_amount = check_eligibility(
                app.member, app.amount_requested
            )
            self.stdout.write(
                f"  App #{app.pk} ({app.member.full_name}) KSh {app.amount_requested:,.0f}: "
                f"{'✓ Eligible' if is_eligible else '✗ ' + reason}"
            )

            if not dry_run and is_eligible:
                app.approve(notes=f"Auto-approved on re-check: {reason}")
                self._generate_plan(app)
                stats["approved"] += 1
            else:
                stats["pending"] += 1

    def _generate_plan(self, app):
        """Create or update the RepaymentPlan for an approved application."""
        from loans.models import RepaymentPlan
        from loans.voice_application import _calculate_installment, DEFAULT_INTEREST_RATE

        installment    = _calculate_installment(app.amount_requested, app.preferred_repayment_period)
        interest_rate  = DEFAULT_INTEREST_RATE / 100
        interest_total = app.amount_requested * interest_rate
        total_repay    = (app.amount_requested + interest_total).quantize(Decimal("0.01"))
        first_payment  = date.today() + relativedelta(months=1)
        last_payment   = first_payment + relativedelta(months=app.preferred_repayment_period - 1)

        plan, created = RepaymentPlan.objects.update_or_create(
            loan_application=app,
            defaults={
                "monthly_installment":  installment,
                "total_repayment":      total_repay,
                "interest_total":       interest_total.quantize(Decimal("0.01")),
                "first_payment_date":   first_payment,
                "last_payment_date":    last_payment,
                "interest_rate_annual": DEFAULT_INTEREST_RATE,
            },
        )
        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"    {action} repayment plan: KSh {installment:,.2f}/mo "
                f"× {app.preferred_repayment_period} months"
            )
        )

    def _send_summary(self, stats: dict, total: int):
        manager_email = getattr(settings, "SACCO_MANAGER_EMAIL", None)
        if not manager_email:
            return
        try:
            send_mail(
                subject="[SACCO] Loan Application Processing Summary",
                message=(
                    f"process_loan_applications completed.\n\n"
                    f"Total reviewed:  {total}\n"
                    f"Auto-approved:   {stats['approved']}\n"
                    f"Still pending:   {stats['pending']}\n"
                    f"Errors:          {stats['errors']}\n\n"
                    f"Review applications at:\n"
                    f"https://your-domain.com/loans/applications/\n"
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@sacco.co.ke"),
                recipient_list=[manager_email],
                fail_silently=True,
            )
        except Exception as exc:   # pylint: disable=broad-except
            logger.error("Summary email failed: %s", exc)
