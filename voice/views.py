"""
voice/views.py

Webhook handlers for Africa's Talking Voice API and a test endpoint
for verifying the Claude integration.

Endpoints
---------
POST /api/voice/callback/   - Initial call webhook; plays bilingual greeting
                              and starts recording.
POST /api/voice/process/    - Receives the recording URL; transcribes and
                              sends to Claude for intent + response.
GET  /api/voice/test-claude/ - Developer test: pass ?text=... to hit Claude
                               directly and see JSON output.
"""

import json
import logging

import requests
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.shortcuts import render
from .models import CallLog
from .claude_client import understand_with_claude

logger = logging.getLogger(__name__)

# ── XML helpers ───────────────────────────────────────────────────────────────

def _xml_response(xml_body: str) -> HttpResponse:
    """Wrap an XML string in an HttpResponse with the correct content type."""
    return HttpResponse(
        f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}',
        content_type="application/xml",
    )


BILINGUAL_GREETING = (
    "Karibu kwenye huduma yetu. "
    "Welcome to our service. "
    "Tafadhali sema swali lako baada ya mlio. "
    "Please say your question after the beep."
)

# ── Views ─────────────────────────────────────────────────────────────────────

def test_page(request):
    """Simple HTML test page for the voice platform"""
    return render(request, 'voice/test.html')

@csrf_exempt
def voice_callback(request):
    """
    Africa's Talking initial call webhook.

    Africa's Talking POSTs call metadata here when a caller dials our
    virtual number.  We:
      1. Create a CallLog entry.
      2. Return XML that plays a bilingual (Kiswahili + English) greeting
         and starts recording the caller's speech (max 30 s).

    Expected POST params (from Africa's Talking):
        sessionId, callerNumber, destinationNumber, direction, callerName (optional)
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    session_id = request.POST.get("sessionId", "")
    caller_number = request.POST.get("callerNumber", "")

    logger.info("Incoming call — session: %s  caller: %s", session_id, caller_number)

    # Persist the call record
    try:
        CallLog.objects.get_or_create(
            session_id=session_id,
            defaults={
                "caller_number": caller_number,
                "status": "received",
            },
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to create CallLog: %s", exc, exc_info=True)

    xml = (
        "<Response>"
        f'  <Say voice="woman">{BILINGUAL_GREETING}</Say>'
        '  <Record'
        '    maxLength="30"'
        '    finishOnKey="#"'
        '    trimSilence="true"'
        '    action="/api/voice/process/"'
        "  />"
        "</Response>"
    )
    return _xml_response(xml)


@csrf_exempt
def process_voice(request):
    """
    Africa's Talking recording webhook.

    Called after the caller has spoken and their audio has been recorded.
    We:
      1. Retrieve the recording URL and session metadata.
      2. (Placeholder) Download the audio file.
      3. (Placeholder) Transcribe audio → text via Africa's Talking STT.
      4. Send the transcript to Claude for language detection, intent
         classification, and a natural-language response.
      5. Update the CallLog.
      6. Return XML that speaks Claude's response back to the caller.

    Expected POST params (from Africa's Talking):
        sessionId, callerNumber, recordingUrl, durationInSeconds
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    session_id = request.POST.get("sessionId", "")
    caller_number = request.POST.get("callerNumber", "")
    recording_url = request.POST.get("recordingUrl", "")
    duration_str = request.POST.get("durationInSeconds", "0")

    logger.info(
        "Processing recording — session: %s  url: %s", session_id, recording_url
    )

    # ── Fetch or create CallLog ──────────────────────────────────────────────
    try:
        call_log, _ = CallLog.objects.get_or_create(
            session_id=session_id,
            defaults={"caller_number": caller_number},
        )
        call_log.status = "processing"
        call_log.recording_url = recording_url
        call_log.duration_seconds = int(duration_str) if duration_str.isdigit() else 0
        call_log.save(update_fields=["status", "recording_url", "duration_seconds"])
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("CallLog fetch/update failed: %s", exc, exc_info=True)
        call_log = None

    # ── Step 1: Download audio (PLACEHOLDER) ────────────────────────────────
    # TODO: Download from recording_url once Africa's Talking provides
    #       authenticated download URLs.
    #
    # Example (unauthenticated):
    #   audio_response = requests.get(recording_url, timeout=30)
    #   audio_bytes = audio_response.content
    #
    audio_bytes = None  # placeholder

    # ── Step 2: Speech-to-text (PLACEHOLDER) ────────────────────────────────
    # TODO: Replace with Africa's Talking STT or another provider.
    #
    # Africa's Talking STT example:
    #   stt_response = requests.post(
    #       "https://voice.africastalking.com/speech/stt",
    #       headers={"apiKey": settings.AT_API_KEY},
    #       files={"audio": ("recording.wav", audio_bytes, "audio/wav")},
    #   )
    #   transcript = stt_response.json().get("text", "")
    #
    # For development/testing, you can pass ?transcript=... in the POST body:
    transcript = request.POST.get("transcript", "")

    if not transcript:
        logger.warning(
            "No transcript available for session %s — STT not yet implemented",
            session_id,
        )
        # Fall back to a generic bilingual prompt
        transcript = request.POST.get("transcript", "")

    # ── Step 3: Understand with Claude ──────────────────────────────────────
    try:
        claude_result = understand_with_claude(transcript)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Claude call failed: %s", exc, exc_info=True)
        claude_result = {
            "intent": "speak_to_agent",
            "amount": None,
            "language": "en",
            "response_text": (
                "Sorry, we are experiencing a technical issue. "
                "Please call back shortly."
            ),
        }

    response_text = claude_result.get("response_text", "")
    intent = claude_result.get("intent", "")
    language = claude_result.get("language", "")

    # ── Step 4: Persist results ──────────────────────────────────────────────
    if call_log:
        try:
            call_log.transcript = transcript
            call_log.intent_detected = intent
            call_log.language_detected = language
            call_log.response_given = response_text
            call_log.status = "completed"
            call_log.save(
                update_fields=[
                    "transcript",
                    "intent_detected",
                    "language_detected",
                    "response_given",
                    "status",
                ]
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to save Claude results to CallLog: %s", exc, exc_info=True)

    # ── Step 5: Build TTS response ───────────────────────────────────────────
    # Sanitise response_text to avoid breaking XML
    safe_response = (
        response_text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

    xml = (
        "<Response>"
        f'  <Say voice="woman">{safe_response}</Say>'
        "</Response>"
    )
    return _xml_response(xml)


def test_claude(request):
    """
    Developer test endpoint — verifies the Claude integration end-to-end.

    Usage:
        GET /api/voice/test-claude/?text=Salio yangu ni ngapi
        GET /api/voice/test-claude/?text=I want to apply for a loan of 50000

    Returns a JSON object with Claude's structured output so you can confirm
    language detection, intent classification, and response generation are
    working correctly before going live.
    """
    text = request.GET.get("text", "").strip()

    if not text:
        return JsonResponse(
            {
                "error": "Please provide a 'text' query parameter.",
                "example": "/api/voice/test-claude/?text=Nataka mkopo",
            },
            status=400,
        )

    logger.info("test_claude called with text: %s", text)

    try:
        result = understand_with_claude(text)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("test_claude error: %s", exc, exc_info=True)
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse(
        {
            "input_text": text,
            "claude_result": result,
        }
    )
