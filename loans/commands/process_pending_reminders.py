"""
loans/management/commands/process_pending_reminders.py

Processes pending reminder records by placing outbound calls.
Should run shortly after check_due_payments (e.g. 06:00 Nairobi time).

Cron:
    0 6 * * * /home/ubuntu/sacco_voice/venv/bin/python \
              /home/ubuntu/sacco_voice/manage.py process_pending_reminders \
              >> /var/log/sacco/process_reminders.log 2>&1

Usage
-----
python manage.py process_pending_reminders
python manage.py process_pending_reminders --limit 50   # cap calls in one run
python manage.py process_pending_reminders --dry-run
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process pending PaymentReminder records and place outbound calls."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Maximum number of calls to place in a single run (default: 200)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show which reminders would be called without placing calls",
        )
        parser.add_argument(
            "--reminder-id",
            type=int,
            default=None,
            help="Process a single specific reminder by PK (for manual retry)",
        )

    def handle(self, *args, **options):
        from loans.scheduler import (
            build_call_queue,
            is_within_dnd,
            calls_today,
            max_calls_allowed,
        )
        from loans.voice_service import initiate_reminder_call

        dry_run     = options["dry_run"]
        limit       = options["limit"]
        specific_id = options["reminder_id"]

        # ── Single-reminder mode ──────────────────────────────────────────────
        if specific_id:
            self.stdout.write(f"Processing single reminder #{specific_id} …")
            if dry_run:
                self.stdout.write(self.style.WARNING("DRY RUN — no call placed"))
                return
            success = initiate_reminder_call(specific_id)
            if success:
                self.stdout.write(self.style.SUCCESS(f"Call placed for reminder #{specific_id}"))
            else:
                self.stdout.write(self.style.ERROR(f"Call failed for reminder #{specific_id}"))
            return

        # ── Batch mode ────────────────────────────────────────────────────────
        queue    = build_call_queue()
        eligible = []

        for reminder in queue:
            if len(eligible) >= limit:
                break
            member = reminder.member
            if is_within_dnd(member):
                self.stdout.write(
                    f"  Skipping #{reminder.pk} ({member.full_name}) — DND hours"
                )
                continue
            if calls_today(member) >= max_calls_allowed(member):
                self.stdout.write(
                    f"  Skipping #{reminder.pk} ({member.full_name}) — daily cap reached"
                )
                continue
            eligible.append(reminder)

        self.stdout.write(
            f"Queue: {len(list(queue))} pending | "
            f"Eligible this run: {len(eligible)} | "
            f"Limit: {limit}"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no calls placed"))
            for r in eligible:
                self.stdout.write(
                    f"  Would call {r.member.full_name} ({r.member.phone_number}) "
                    f"— {r.reminder_type} | attempt {r.attempts + 1}/{r.max_attempts}"
                )
            return

        # ── Place calls with rate limiting ────────────────────────────────────
        import time
        MAX_PER_MINUTE = 10
        stats = {"attempted": 0, "succeeded": 0, "failed": 0}
        batch_count = 0
        batch_start = time.monotonic()

        for reminder in eligible:
            if batch_count >= MAX_PER_MINUTE:
                elapsed = time.monotonic() - batch_start
                sleep_s = max(0.0, 60.0 - elapsed)
                if sleep_s > 0:
                    self.stdout.write(f"Rate limit — sleeping {sleep_s:.1f}s …")
                    time.sleep(sleep_s)
                batch_count = 0
                batch_start = time.monotonic()

            try:
                success = initiate_reminder_call(reminder.pk)
                stats["attempted"] += 1
                batch_count += 1
                if success:
                    stats["succeeded"] += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  ✓ Called {reminder.member.full_name} "
                            f"({reminder.member.phone_number})"
                        )
                    )
                else:
                    stats["failed"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"  ✗ Failed to call {reminder.member.full_name}"
                        )
                    )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Error on reminder #%d: %s", reminder.pk, exc, exc_info=True)
                stats["failed"] += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. Attempted: {stats['attempted']} | "
                f"Succeeded: {stats['succeeded']} | "
                f"Failed: {stats['failed']}"
            )
        )
