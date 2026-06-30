"""
loans/models.py

Core data models for the SACCO loan, reminder, and voice application system.

Models
------
Member             — SACCO member with contact details and language preference
MemberPreference   — DND hours and call limits per member
Loan               — Active loan with payment schedule
PaymentReminder    — Individual outbound reminder call record
ReminderLog        — Immutable audit trail for reminders
LoanApplication    — Voice loan application submitted by a member
EligibilityCheck   — Automated eligibility result for a loan application
RepaymentPlan      — Generated repayment schedule for an approved application
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


# ── Member ─────────────────────────────────────────────────────────────────────

class Member(models.Model):
    """A registered SACCO member who may hold one or more loans."""

    LANGUAGE_CHOICES = [
        ("sw", "Kiswahili"),
        ("en", "English"),
    ]

    phone_number = models.CharField(
        max_length=20, unique=True,
        help_text="E.164 format — e.g. +254712345678",
    )
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    preferred_language = models.CharField(
        max_length=5, choices=LANGUAGE_CHOICES, default="sw",
    )
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.phone_number})"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


# ── MemberPreference ───────────────────────────────────────────────────────────

class MemberPreference(models.Model):
    """Per-member calling preferences (DND, call cap, opt-out)."""

    member = models.OneToOneField(
        Member, on_delete=models.CASCADE, related_name="preference",
    )
    do_not_disturb_start = models.TimeField(default="22:00")
    do_not_disturb_end   = models.TimeField(default="06:00")
    max_calls_per_day    = models.IntegerField(default=3)
    opt_out = models.BooleanField(default=False)

    def __str__(self):
        return f"Prefs for {self.member.full_name}"


# ── Loan ───────────────────────────────────────────────────────────────────────

class Loan(models.Model):
    """Represents a single disbursed loan."""

    STATUS_CHOICES = [
        ("active",    "Active"),
        ("paid",      "Fully Paid"),
        ("defaulted", "Defaulted"),
        ("suspended", "Suspended"),
    ]

    member              = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="loans")
    principal_amount    = models.DecimalField(max_digits=12, decimal_places=2)
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2)
    monthly_installment = models.DecimalField(max_digits=10, decimal_places=2)
    next_payment_date   = models.DateField(db_index=True)
    due_day_of_month    = models.IntegerField()
    status              = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active", db_index=True)
    interest_rate_annual = models.DecimalField(max_digits=5, decimal_places=2, default=12.00)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_payment_date"]

    def __str__(self):
        return (
            f"Loan #{self.pk} — {self.member.full_name} | "
            f"KSh {self.outstanding_balance:,.0f} due {self.next_payment_date}"
        )

    @property
    def is_overdue(self) -> bool:
        from datetime import date
        return self.status == "active" and self.next_payment_date < date.today()


# ── PaymentReminder ────────────────────────────────────────────────────────────

class PaymentReminder(models.Model):
    """Outbound call reminder for an upcoming or overdue loan payment."""

    REMINDER_TYPE_CHOICES = [
        ("pre_due",   "Pre-Due (1–3 days before)"),
        ("due_today", "Due Today"),
        ("overdue",   "Overdue"),
    ]
    STATUS_CHOICES = [
        ("pending",   "Pending"),
        ("calling",   "Call in Progress"),
        ("sent",      "Call Sent / Ringing"),
        ("completed", "Completed"),
        ("failed",    "Failed"),
        ("cancelled", "Cancelled"),
    ]

    loan   = models.ForeignKey(Loan,   on_delete=models.CASCADE, related_name="reminders")
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="reminders")

    reminder_date     = models.DateField(db_index=True)
    reminder_type     = models.CharField(max_length=20, choices=REMINDER_TYPE_CHOICES)
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    call_sid          = models.CharField(max_length=100, blank=True)
    response_received = models.TextField(blank=True)
    payment_confirmed = models.BooleanField(default=False)
    attempts          = models.IntegerField(default=0)
    max_attempts      = models.IntegerField(default=3)
    call_duration_seconds = models.IntegerField(default=0)
    call_cost_kes     = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    created_at        = models.DateTimeField(auto_now_add=True)
    completed_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("loan", "reminder_date", "reminder_type")]

    def __str__(self):
        return f"Reminder #{self.pk} [{self.reminder_type}] {self.member.full_name} — {self.status}"

    def mark_completed(self, payment_confirmed: bool = False):
        self.status = "completed"
        self.payment_confirmed = payment_confirmed
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "payment_confirmed", "completed_at"])

    def mark_failed(self):
        self.status = "failed"
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts and self.status in ("failed", "pending")


# ── ReminderLog ────────────────────────────────────────────────────────────────

class ReminderLog(models.Model):
    """Immutable audit trail for PaymentReminder events."""

    ACTION_CHOICES = [
        ("call_initiated",    "Call Initiated"),
        ("call_answered",     "Call Answered"),
        ("call_no_answer",    "No Answer"),
        ("call_busy",         "Line Busy"),
        ("call_failed",       "Call Failed"),
        ("call_completed",    "Call Completed"),
        ("payment_confirmed", "Payment Confirmed by Member"),
        ("payment_denied",    "Member Will Not Pay"),
        ("retry_scheduled",   "Retry Scheduled"),
        ("cancelled",         "Reminder Cancelled"),
    ]

    reminder   = models.ForeignKey(PaymentReminder, on_delete=models.CASCADE, related_name="logs")
    action     = models.CharField(max_length=30, choices=ACTION_CHOICES)
    notes      = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.reminder_id} → {self.action}"


# ══════════════════════════════════════════════════════════════════════════════
# LOAN APPLICATION MODELS
# ══════════════════════════════════════════════════════════════════════════════

class LoanApplication(models.Model):
    """
    A loan application submitted by a member via voice.

    Lifecycle
    ---------
    pending_review  — Submitted, awaiting human or automated review
    approved        — Approved (by automated rules or SACCO manager)
    rejected        — Rejected (by automated rules or SACCO manager)
    disbursed       — Funds sent to the member
    cancelled       — Member or staff cancelled the application

    The full conversation transcript and Claude's structured JSON analysis
    are stored for audit and model-improvement purposes.
    """

    APPLICATION_STATUS_CHOICES = [
        ("pending_review", "Pending Review"),
        ("approved",       "Approved"),
        ("rejected",       "Rejected"),
        ("disbursed",      "Disbursed"),
        ("cancelled",      "Cancelled"),
    ]

    # ── Core fields ───────────────────────────────────────────────────────────
    member   = models.ForeignKey(
        Member, on_delete=models.PROTECT, related_name="loan_applications",
    )
    amount_requested = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Amount the member requested in KSh",
    )
    purpose = models.TextField(
        blank=True,
        help_text="What the member said they need the loan for",
    )
    preferred_repayment_period = models.IntegerField(
        default=12,
        help_text="Preferred loan term in months",
    )
    status = models.CharField(
        max_length=20,
        choices=APPLICATION_STATUS_CHOICES,
        default="pending_review",
        db_index=True,
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    application_date = models.DateTimeField(auto_now_add=True, db_index=True)

    # ── Review ────────────────────────────────────────────────────────────────
    reviewed_by   = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_applications",
    )
    reviewed_at   = models.DateTimeField(null=True, blank=True)
    review_notes  = models.TextField(blank=True)

    # ── Voice conversation data ───────────────────────────────────────────────
    transcript_original = models.TextField(
        blank=True,
        help_text="Full conversation transcript from the voice application call",
    )
    session_id = models.CharField(
        max_length=100, blank=True,
        help_text="Africa's Talking session ID for the application call",
    )
    caller_number = models.CharField(
        max_length=20, blank=True,
        help_text="Phone number used to make the application call",
    )

    # ── Claude analysis ───────────────────────────────────────────────────────
    claude_analysis = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Claude's structured extraction: "
            "{amount, purpose, period, confidence, language, notes}"
        ),
    )

    # ── Conversation state (used during the live call) ────────────────────────
    conversation_state = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Tracks which questions have been asked/answered during the "
            "voice application flow. Cleared after call completes."
        ),
    )

    class Meta:
        ordering = ["-application_date"]
        verbose_name = "Loan Application"
        verbose_name_plural = "Loan Applications"

    def __str__(self):
        return (
            f"App #{self.pk} — {self.member.full_name} | "
            f"KSh {self.amount_requested:,.0f} | {self.status}"
        )

    def approve(self, reviewed_by=None, notes: str = ""):
        """Mark the application as approved and record who reviewed it."""
        self.status      = "approved"
        self.reviewed_by = reviewed_by
        self.reviewed_at = timezone.now()
        self.review_notes = notes
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"])

    def reject(self, reviewed_by=None, notes: str = ""):
        """Mark the application as rejected."""
        self.status      = "rejected"
        self.reviewed_by = reviewed_by
        self.reviewed_at = timezone.now()
        self.review_notes = notes
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"])

    @property
    def is_complete(self) -> bool:
        """True when all required fields have been collected from the member."""
        return bool(self.amount_requested and self.purpose and self.preferred_repayment_period)


class EligibilityCheck(models.Model):
    """
    Automated eligibility assessment for a LoanApplication.

    Computed immediately after the voice application is submitted.
    A human reviewer can override the outcome via LoanApplication.approve/reject.
    """

    loan_application  = models.OneToOneField(
        LoanApplication, on_delete=models.CASCADE, related_name="eligibility",
    )
    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="eligibility_checks",
    )
    is_eligible        = models.BooleanField(default=False)
    max_eligible_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Maximum amount the member qualifies for based on current rules",
    )
    reasons    = models.TextField(blank=True, help_text="Comma-separated list of pass/fail reasons")
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Eligibility Check"

    def __str__(self):
        verdict = "✓ Eligible" if self.is_eligible else "✗ Not eligible"
        return f"{verdict} — App #{self.loan_application_id} ({self.member.full_name})"


class RepaymentPlan(models.Model):
    """
    A generated repayment schedule attached to an approved LoanApplication.
    Created by process_loan_applications management command.
    """

    loan_application   = models.OneToOneField(
        LoanApplication, on_delete=models.CASCADE, related_name="repayment_plan",
    )
    monthly_installment = models.DecimalField(max_digits=10, decimal_places=2)
    total_repayment     = models.DecimalField(max_digits=12, decimal_places=2)
    interest_total      = models.DecimalField(max_digits=12, decimal_places=2)
    first_payment_date  = models.DateField()
    last_payment_date   = models.DateField()
    interest_rate_annual = models.DecimalField(max_digits=5, decimal_places=2, default=12.00)
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Repayment Plan"

    def __str__(self):
        return (
            f"Plan for App #{self.loan_application_id} — "
            f"KSh {self.monthly_installment:,.0f}/mo × "
            f"{self.loan_application.preferred_repayment_period} months"
        )
