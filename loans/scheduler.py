"""
loans/scheduler.py

Scheduling layer that sits between the management commands and the
Africa's Talking voice API.

Responsibilities
----------------
1. schedule_reminder_calls()     — decide which pending reminders are safe to call right now
2. process_pending_reminders()   — iterate the ready queue and fire calls with rate limiting
3. is_within_dnd()               — check whether it is quiet hours for a given member
4. calls_today()                 — enforce per-member daily call cap
5. build_call_queue()            — return an ordered queryset of reminders to process

Rate limiting: max 10 outbound calls per minute (Africa's Talking soft limit).
DND: per-member preference (default 22:00–06:00 Nairobi time).
"""

import logging
import time
from datetime import date, datetime
from typing import List

from django.utils import timezone

logger = logging.getLogger(__name__)

# Africa's Talking recommends no more than 10 concurrent / per-minute calls
# for free-tier accounts.  Increase for paid accounts.
MAX_CALLS_PER_MINUTE = 10

# Nairobi is UTC+3 — used as the default reference timezone for DND checks
NAIROBI_TZ_OFFSET_HOURS = 3


def get_nairobi_time() -> datetime:
    """Return the current time in Africa/Nairobi (UTC+3)."""
    from datetime import timezone as dt_tz, timedelta
    nairobi_tz = dt_tz(timedelta(hours=NAIROBI_TZ_OFFSET_HOURS))
    return datetime.now(tz=nairobi_tz)


def is_within_dnd(member) -> bool:
    """
    Return True if the current Nairobi time falls within this member's
    Do-Not-Disturb window.

    Handles overnight windows correctly (e.g. 22:00 → 06:00).

    Args:
        member: A Member model instance (with optional .preference relation).

    Returns:
        bool — True means DO NOT call; False means calling is allowed.
    """
    # If member opted out entirely, treat as always-DND
    try:
        pref = member.preference
        if pref.opt_out:
            return True
        dnd_start = pref.do_not_disturb_start
        dnd_end   = pref.do_not_disturb_end
    except Exception:  # pylint: disable=broad-except
        # No preference row — use sensible defaults
        from datetime import time as t
        dnd_start = t(22, 0)
        dnd_end   = t(6, 0)

    now_time = get_nairobi_time().time()

    # Overnight window: start > end  (e.g. 22:00 → 06:00)
    if dnd_start > dnd_end:
        return now_time >= dnd_start or now_time < dnd_end
    # Same-day window: start < end  (e.g. 13:00 → 14:00)
    return dnd_start <= now_time < dnd_end


def calls_today(member) -> int:
    """
    Count how many reminder calls have already been attempted for this
    member today (regardless of outcome).

    Args:
        member: A Member model instance.

    Returns:
        int — number of call attempts made today.
    """
    from loans.models import PaymentReminder
    today = date.today()
    return (
        PaymentReminder.objects.filter(
            member=member,
            reminder_date=today,
            attempts__gt=0,
        ).count()
    )


def max_calls_allowed(member) -> int:
    """Return the member's daily call cap from their preference (default 3)."""
    try:
        return member.preference.max_calls_per_day
    except Exception:  # pylint: disable=broad-except
        return 3


def build_call_queue():
    """
    Return a queryset of PaymentReminder rows that are ready to call RIGHT NOW:
    - status = 'pending'
    - reminder_date = today (or earlier, for retries)
    - can still be retried (attempts < max_attempts)

    Ordered by reminder_type priority: overdue → due_today → pre_due
    """
    from loans.models import PaymentReminder
    today = date.today()

    TYPE_ORDER = {"overdue": 0, "due_today": 1, "pre_due": 2}

    qs = (
        PaymentReminder.objects.filter(
            status="pending",
            reminder_date__lte=today,
        )
        .select_related("member", "member__preference", "loan")
        .filter(attempts__lt=models_max_attempts())
    )
    # Sort in Python so we can use the priority dict
    return sorted(qs, key=lambda r: TYPE_ORDER.get(r.reminder_type, 9))


def models_max_attempts():
    """
    Inline helper — avoids importing models at module level.
    Returns the maximum attempts value used in the filter.
    """
    # We use 3 as the system-wide ceiling; individual reminders may have lower values.
    return 3


def schedule_reminder_calls() -> int:
    """
    Entry point called by the check_due_payments management command.

    Evaluates all pending reminders and marks those that pass DND / daily-cap
    checks as ready (status remains 'pending' — process_pending_reminders
    will fire the actual calls).

    Returns:
        int — number of reminders scheduled (i.e. not filtered out).
    """
    from loans.models import PaymentReminder, ReminderLog

    queue = build_call_queue()
    scheduled = 0

    for reminder in queue:
        member = reminder.member

        # DND check
        if is_within_dnd(member):
            logger.debug(
                "Skipping reminder #%d for %s — DND hours",
                reminder.pk, member.full_name,
            )
            continue

        # Daily cap check
        if calls_today(member) >= max_calls_allowed(member):
            logger.debug(
                "Skipping reminder #%d for %s — daily cap reached",
                reminder.pk, member.full_name,
            )
            continue

        scheduled += 1

    logger.info("schedule_reminder_calls: %d reminders ready to send", scheduled)
    return scheduled


def process_pending_reminders() -> dict:
    """
    Iterate the call queue and place outbound calls via voice_service.
    Respects the per-minute rate limit using time.sleep().

    Returns:
        dict with keys: attempted, succeeded, failed, skipped
    """
    from loans.voice_service import initiate_reminder_call

    queue = build_call_queue()
    stats = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0}

    batch: List = []
    for reminder in queue:
        member = reminder.member

        if is_within_dnd(member):
            stats["skipped"] += 1
            continue
        if calls_today(member) >= max_calls_allowed(member):
            stats["skipped"] += 1
            continue

        batch.append(reminder)

    logger.info(
        "process_pending_reminders: %d in queue, %d eligible",
        len(list(queue)), len(batch),
    )

    call_count_this_minute = 0
    minute_start = time.monotonic()

    for reminder in batch:
        # Rate-limit enforcement
        elapsed = time.monotonic() - minute_start
        if call_count_this_minute >= MAX_CALLS_PER_MINUTE:
            sleep_for = max(0.0, 60.0 - elapsed)
            logger.info(
                "Rate limit reached (%d calls/min) — sleeping %.1fs",
                MAX_CALLS_PER_MINUTE, sleep_for,
            )
            time.sleep(sleep_for)
            call_count_this_minute = 0
            minute_start = time.monotonic()

        try:
            success = initiate_reminder_call(reminder.pk)
            stats["attempted"] += 1
            call_count_this_minute += 1
            if success:
                stats["succeeded"] += 1
            else:
                stats["failed"] += 1
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Error initiating call for reminder #%d: %s",
                reminder.pk, exc, exc_info=True,
            )
            stats["failed"] += 1

    logger.info("process_pending_reminders result: %s", stats)
    return stats
