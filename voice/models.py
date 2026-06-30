"""
voice/models.py

CallLog model for tracking all voice interactions on the SACCO platform.
Stores caller info, transcripts, detected language/intent, and AI responses.
"""

from django.db import models


class CallLog(models.Model):
    """
    Records each inbound voice call from a SACCO member.
    Tracks the full lifecycle: received → processing → completed/failed.
    """

    STATUS_CHOICES = [
        ("received", "Received"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    LANGUAGE_CHOICES = [
        ("sw", "Kiswahili"),
        ("en", "English"),
        ("mixed", "Mixed"),
        ("", "Unknown"),
    ]

    caller_number = models.CharField(
        max_length=20,
        help_text="Caller's phone number in E.164 format, e.g. +254712345678",
    )
    session_id = models.CharField(
        max_length=100,
        unique=True,
        help_text="Africa's Talking session ID — unique per call",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="received",
    )
    recording_url = models.URLField(
        blank=True,
        help_text="URL of the caller's recorded audio from Africa's Talking",
    )
    transcript = models.TextField(
        blank=True,
        help_text="Speech-to-text transcript of what the caller said",
    )
    language_detected = models.CharField(
        max_length=10,
        blank=True,
        choices=LANGUAGE_CHOICES,
        help_text="Language detected by Claude: 'sw', 'en', or 'mixed'",
    )
    intent_detected = models.CharField(
        max_length=50,
        blank=True,
        help_text=(
            "Intent classified by Claude, e.g. check_balance, apply_loan, etc."
        ),
    )
    response_given = models.TextField(
        blank=True,
        help_text="The text response spoken back to the caller via TTS",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.IntegerField(
        default=0,
        help_text="Call duration in seconds (populated after call ends)",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Call Log"
        verbose_name_plural = "Call Logs"

    def __str__(self):
        return f"{self.caller_number} | {self.session_id} | {self.status}"
