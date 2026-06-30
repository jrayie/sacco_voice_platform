"""
loans/admin.py

Django admin for the loans, reminders, and voice application system.
"""

from datetime import date

from django.contrib import admin, messages as django_messages
from django.utils.html import format_html
from django.utils import timezone

from .models import (
    Member, MemberPreference, Loan,
    PaymentReminder, ReminderLog,
    LoanApplication, EligibilityCheck, RepaymentPlan,
)


# ── Member ─────────────────────────────────────────────────────────────────────

class MemberPreferenceInline(admin.StackedInline):
    model = MemberPreference
    can_delete = False
    extra = 0


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display  = ("full_name", "phone_number", "preferred_language", "is_active", "loan_count", "app_count")
    list_filter   = ("preferred_language", "is_active")
    search_fields = ("first_name", "last_name", "phone_number")
    inlines       = [MemberPreferenceInline]

    @admin.display(description="Active Loans")
    def loan_count(self, obj):
        return obj.loans.filter(status="active").count()

    @admin.display(description="Applications")
    def app_count(self, obj):
        return obj.loan_applications.count()


# ── Loan ───────────────────────────────────────────────────────────────────────

@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display    = ("id", "member_link", "outstanding_fmt", "monthly_installment", "next_payment_date", "due_status", "status")
    list_filter     = ("status",)
    search_fields   = ("member__first_name", "member__last_name", "member__phone_number")
    readonly_fields = ("created_at", "updated_at")
    ordering        = ("next_payment_date",)

    @admin.display(description="Member", ordering="member__last_name")
    def member_link(self, obj):
        return format_html('<a href="/admin/loans/member/{}/change/">{}</a>',
                           obj.member.pk, obj.member.full_name)

    @admin.display(description="Outstanding")
    def outstanding_fmt(self, obj):
        return f"KSh {obj.outstanding_balance:,.0f}"

    @admin.display(description="Payment Status")
    def due_status(self, obj):
        today = date.today()
        npd   = obj.next_payment_date
        if npd < today:
            return format_html('<span style="color:#dc2626;font-weight:600;">⚠ {}d overdue</span>',
                               (today - npd).days)
        elif npd == today:
            return format_html('<span style="color:#d97706;font-weight:600;">Due Today</span>')
        elif (npd - today).days <= 3:
            return format_html('<span style="color:#2563eb;">Due in {}d</span>', (npd - today).days)
        return format_html('<span style="color:#16a34a;">Due {}</span>', npd.strftime("%b %d"))


# ── PaymentReminder ────────────────────────────────────────────────────────────

class ReminderLogInline(admin.TabularInline):
    model = ReminderLog
    extra = 0
    can_delete = False
    readonly_fields = ("action", "notes", "created_at")
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PaymentReminder)
class PaymentReminderAdmin(admin.ModelAdmin):
    list_display  = ("id", "member_name", "reminder_type", "reminder_date", "status_badge", "payment_confirmed", "attempts")
    list_filter   = ("status", "reminder_type", "payment_confirmed")
    search_fields = ("member__first_name", "member__last_name", "member__phone_number")
    readonly_fields = ("created_at", "completed_at", "call_sid")
    ordering      = ("-created_at",)
    inlines       = [ReminderLogInline]
    actions       = ["trigger_call_now", "mark_cancelled"]

    @admin.display(description="Member")
    def member_name(self, obj):
        return obj.member.full_name

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending": ("#2563eb", "#dbeafe"), "calling": ("#d97706", "#fef3c7"),
            "sent": ("#d97706", "#fef3c7"),    "completed": ("#16a34a", "#dcfce7"),
            "failed": ("#dc2626", "#fee2e2"),  "cancelled": ("#6b7280", "#f3f4f6"),
        }
        fg, bg = colours.get(obj.status, ("#6b7280", "#f3f4f6"))
        return format_html(
            '<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:12px;'
            'font-size:0.82em;font-weight:600;">{label}</span>',
            bg=bg, fg=fg, label=obj.get_status_display(),
        )

    @admin.action(description="📞 Trigger outbound call NOW")
    def trigger_call_now(self, request, queryset):
        from loans.voice_service import initiate_reminder_call
        ok = fail = 0
        for r in queryset:
            if r.status in ("completed", "cancelled", "calling"):
                fail += 1; continue
            r.status = "pending"; r.save(update_fields=["status"])
            if initiate_reminder_call(r.pk): ok += 1
            else: fail += 1
        self.message_user(request, f"{ok} call(s) placed, {fail} skipped.",
                          django_messages.SUCCESS if ok else django_messages.WARNING)

    @admin.action(description="✗ Cancel selected")
    def mark_cancelled(self, request, queryset):
        n = queryset.filter(status="pending").update(status="cancelled")
        self.message_user(request, f"{n} reminder(s) cancelled.")


# ════════════════════════════════════════════════════════════════════════════════
# LOAN APPLICATION ADMIN
# ════════════════════════════════════════════════════════════════════════════════

class EligibilityInline(admin.StackedInline):
    model = EligibilityCheck
    can_delete = False
    extra = 0
    readonly_fields = ("is_eligible", "max_eligible_amount", "reasons", "checked_at")

    def has_add_permission(self, request, obj=None):
        return False


class RepaymentPlanInline(admin.StackedInline):
    model = RepaymentPlan
    can_delete = False
    extra = 0
    readonly_fields = (
        "monthly_installment", "total_repayment", "interest_total",
        "first_payment_date", "last_payment_date", "interest_rate_annual", "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LoanApplication)
class LoanApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "member_name", "amount_fmt", "purpose_short",
        "preferred_repayment_period", "status_badge",
        "application_date", "reviewed_by",
    )
    list_filter   = ("status", "application_date")
    search_fields = (
        "member__first_name", "member__last_name",
        "member__phone_number", "purpose",
    )
    readonly_fields = (
        "application_date", "transcript_original", "claude_analysis",
        "session_id", "caller_number", "conversation_state",
        "reviewed_at",
    )
    ordering     = ("-application_date",)
    inlines      = [EligibilityInline, RepaymentPlanInline]
    actions      = ["approve_applications", "reject_applications"]
    date_hierarchy = "application_date"

    fieldsets = (
        ("Applicant", {
            "fields": ("member", "caller_number", "session_id"),
        }),
        ("Loan Request", {
            "fields": ("amount_requested", "purpose", "preferred_repayment_period", "status"),
        }),
        ("Review", {
            "fields": ("reviewed_by", "reviewed_at", "review_notes"),
        }),
        ("Voice Transcript", {
            "fields": ("transcript_original",),
            "classes": ("collapse",),
        }),
        ("Claude Analysis", {
            "fields": ("claude_analysis",),
            "classes": ("collapse",),
        }),
        ("Conversation State (debug)", {
            "fields": ("conversation_state",),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Member")
    def member_name(self, obj):
        return obj.member.full_name

    @admin.display(description="Amount")
    def amount_fmt(self, obj):
        return f"KSh {obj.amount_requested:,.0f}"

    @admin.display(description="Purpose")
    def purpose_short(self, obj):
        return (obj.purpose[:50] + "…") if len(obj.purpose) > 50 else obj.purpose

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending_review": ("#d97706", "#fef3c7"),
            "approved":       ("#16a34a", "#dcfce7"),
            "rejected":       ("#dc2626", "#fee2e2"),
            "disbursed":      ("#2563eb", "#dbeafe"),
            "cancelled":      ("#6b7280", "#f3f4f6"),
        }
        fg, bg = colours.get(obj.status, ("#6b7280", "#f3f4f6"))
        return format_html(
            '<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:12px;'
            'font-size:0.82em;font-weight:600;">{label}</span>',
            bg=bg, fg=fg, label=obj.get_status_display(),
        )

    @admin.action(description="✓ Approve selected applications")
    def approve_applications(self, request, queryset):
        approved = 0
        for app in queryset.filter(status="pending_review"):
            app.approve(reviewed_by=request.user, notes="Approved via admin action.")
            approved += 1
        self.message_user(
            request, f"{approved} application(s) approved.",
            django_messages.SUCCESS,
        )

    @admin.action(description="✗ Reject selected applications")
    def reject_applications(self, request, queryset):
        rejected = 0
        for app in queryset.filter(status="pending_review"):
            app.reject(reviewed_by=request.user, notes="Rejected via admin action.")
            rejected += 1
        self.message_user(
            request, f"{rejected} application(s) rejected.",
            django_messages.WARNING,
        )


@admin.register(EligibilityCheck)
class EligibilityCheckAdmin(admin.ModelAdmin):
    list_display  = ("id", "member_name", "is_eligible", "max_eligible_amount", "reasons_short", "checked_at")
    list_filter   = ("is_eligible",)
    search_fields = ("member__first_name", "member__last_name")
    readonly_fields = ("checked_at",)

    @admin.display(description="Member")
    def member_name(self, obj):
        return obj.member.full_name

    @admin.display(description="Reasons")
    def reasons_short(self, obj):
        return (obj.reasons[:80] + "…") if len(obj.reasons) > 80 else obj.reasons

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RepaymentPlan)
class RepaymentPlanAdmin(admin.ModelAdmin):
    list_display  = ("id", "app_link", "monthly_installment", "total_repayment", "first_payment_date", "last_payment_date")
    readonly_fields = ("created_at",)

    @admin.display(description="Application")
    def app_link(self, obj):
        return format_html(
            '<a href="/admin/loans/loanapplication/{}/change/">App #{}</a>',
            obj.loan_application_id, obj.loan_application_id,
        )


@admin.register(ReminderLog)
class ReminderLogAdmin(admin.ModelAdmin):
    list_display    = ("id", "reminder", "action", "notes", "created_at")
    list_filter     = ("action",)
    readonly_fields = ("created_at",)
    ordering        = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
