"""
voice/admin.py

Django admin configuration for the SACCO voice platform.
Registers CallLog with a rich interface for monitoring calls,
filtering by status/language/intent, and searching by caller number.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import CallLog


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    """
    Admin interface for CallLog.

    Provides:
    - Colour-coded status badges for quick visual triage
    - Filters by status, language, and intent
    - Full-text search on caller number, session ID, and transcript
    - Read-only fields to prevent accidental edits to call records
    - Sensible column ordering (newest first, via model Meta)
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "caller_number",
        "session_id",
        "status_badge",
        "language_detected",
        "intent_detected",
        "duration_seconds",
        "created_at",
    )

    list_filter = (
        "status",
        "language_detected",
        "intent_detected",
        "created_at",
    )

    search_fields = (
        "caller_number",
        "session_id",
        "transcript",
        "intent_detected",
    )

    date_hierarchy = "created_at"

    ordering = ("-created_at",)

    # Show 25 rows per page — calls can accumulate quickly
    list_per_page = 25

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "caller_number",
        "session_id",
        "recording_url",
        "transcript",
        "language_detected",
        "intent_detected",
        "response_given",
        "created_at",
        "duration_seconds",
    )

    fieldsets = (
        (
            "Call Identification",
            {
                "fields": ("caller_number", "session_id", "status", "created_at"),
            },
        ),
        (
            "Recording & Transcript",
            {
                "fields": ("recording_url", "transcript", "duration_seconds"),
            },
        ),
        (
            "AI Analysis",
            {
                "fields": (
                    "language_detected",
                    "intent_detected",
                    "response_given",
                ),
                "description": (
                    "These fields are populated automatically by Claude via AWS Bedrock."
                ),
            },
        ),
    )

    # ── Custom columns ────────────────────────────────────────────────────────

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        """Render a colour-coded pill badge for the call status."""
        colours = {
            "received":   ("#1a73e8", "#e8f0fe"),   # blue
            "processing": ("#f9a825", "#fff8e1"),   # amber
            "completed":  ("#1e8e3e", "#e6f4ea"),   # green
            "failed":     ("#d93025", "#fce8e6"),   # red
        }
        fg, bg = colours.get(obj.status, ("#5f6368", "#f1f3f4"))
        return format_html(
            '<span style="'
            "background:{bg}; color:{fg}; padding:2px 10px; "
            "border-radius:12px; font-size:0.85em; font-weight:600;"
            '">{label}</span>',
            bg=bg,
            fg=fg,
            label=obj.get_status_display(),
        )

    # ── Permissions ───────────────────────────────────────────────────────────
    # Call logs are audit records — disallow deletion from the admin UI
    # to prevent accidental data loss.  Superusers can still delete via
    # the Django shell if absolutely necessary.

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        """
        Calls are created only by the webhook views, never manually.
        Disable the 'Add' button in the admin.
        """
        return False
