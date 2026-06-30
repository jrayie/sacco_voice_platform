"""
voice/urls.py

Include in config/urls.py:
    path("api/voice/", include("voice.urls")),
"""

from django.urls import path
from . import views, ussd_views

app_name = "voice"

urlpatterns = [
    # ── Inbound call webhooks ──────────────────────────────────────────────
    path("callback/",    views.voice_callback, name="callback"),
    path("process/",     views.process_voice,  name="process"),

    # ── Developer test ─────────────────────────────────────────────────────
    path("test-claude/", views.test_claude, name="test_claude"),

    # ── Outbound reminder webhooks ─────────────────────────────────────────
    path("reminder-callback/", views.reminder_callback, name="reminder_callback"),
    path("reminder-response/", views.reminder_response, name="reminder_response"),

    # ── Phase 1: Feedback collection ───────────────────────────────────────
    #path("feedback/collect/",    feedback_views.collect_feedback,            name="collect_feedback"),
    #path("feedback/escalation/", feedback_views.collect_escalation_feedback, name="collect_escalation"),
    #path("feedback/correction/", feedback_views.collect_correction,          name="collect_correction"),
    #path("feedback/flag/",       feedback_views.flag_for_training,           name="flag_training"),

    # ── USSD handler ───────────────────────────────────────────────────────
    path("ussd/", ussd_views.ussd_callback, name="ussd_callback"),
]
