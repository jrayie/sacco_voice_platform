"""
loans/voice_service.py

Outbound voice call service for payment reminders.

Functions
---------
initiate_reminder_call(reminder_id)
    Places an outbound call to the member via Africa's Talking Voice API.

build_reminder_message(reminder)
    Generates a personalised message in the member's preferred language
    using Claude (via AWS Bedrock).

handle_reminder_response(call_session_id, member_response)
    Processes what the member said and determines if they confirmed payment.
"""

import logging
from datetime import date
from decimal import Decimal

import africastalking  # pip install africastalking
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from voice.claude_client import understand_with_claude

logger = logging.getLogger(__name__)

# ── Africa's Talking setup ────────────────────────────────────────────────────
# Initialise once at import time; credentials come from Django settings.
# settings.py should contain:
#   AT_USERNAME = "your_username"
#   AT_API_KEY  = "your_api_key"
#   AT_VOICE_CALLBACK_URL = "https://your-domain.com/api/voice/reminder-callback/"


def _get_at_voice():
    """Return an initialised Africa's Talking Voice client."""
    africastalking.initialize(
        username=settings.AT_USERNAME,
        api_key=settings.AT_API_KEY,
    )
    return africastalking.Voice()


# ── Message templates ─────────────────────────────────────────────────────────

_TEMPLATES = {
    "sw": {
        "pre_due": (
            "Habari {name}. Hii ni ukumbusho wa mkopo wako wa KSh {amount:,.0f} "
            "kutoka SACCO yetu. Malipo ya KSh {installment:,.0f} yanatarajiwa "
            "tarehe {due_date}. Tafadhali hakikisha kuna salio la kutosha. "
            "Kwa maswali, wasiliana na ofisi yetu. Asante."
        ),
        "due_today": (
            "Habari {name}. Malipo ya mkopo wako wa KSh {installment:,.0f} "
            "yanastahili kulipwa LEO. Tafadhali lipa kupitia M-Pesa au njia "
            "nyingine kabla ya mwisho wa siku. Asante kwa ushirikiano wako."
        ),
        "overdue": (
            "Habari {name}. Malipo ya mkopo wako ya KSh {installment:,.0f} "
            "yalikuwa yanahitajika tarehe {due_date} na bado hayajalipwa. "
            "Tafadhali wasiliana na ofisi yetu haraka ili kuepuka faini. "
            "Asante."
        ),
    },
    "en": {
        "pre_due": (
            "Hello {name}. This is a reminder from your SACCO regarding your loan "
            "of KSh {amount:,.0f}. Your installment of KSh {installment:,.0f} is due "
            "on {due_date}. Please ensure you have sufficient balance in M-Pesa or "
            "your bank account. Contact our office for assistance. Thank you."
        ),
        "due_today": (
            "Hello {name}. Your loan installment of KSh {installment:,.0f} is due "
            "TODAY. Please make your payment via M-Pesa or at our offices before "
            "close of business. Thank you for your cooperation."
        ),
        "overdue": (
            "Hello {name}. Your loan installment of KSh {installment:,.0f} was due "
            "on {due_date} and remains unpaid. Please contact our office immediately "
            "to avoid penalties. Thank you."
        ),
    },
}

# ── Claude prompt for processing member responses ─────────────────────────────

_RESPONSE_SYSTEM_PROMPT = """You are processing a SACCO member's spoken response 
to a loan payment reminder call. Determine whether the member has confirmed they 
will pay or have already paid.

Respond ONLY with valid JSON:
{
    "payment_confirmed": true or false,
    "intent": "will_pay" | "already_paid" | "cannot_pay" | "dispute" | "other",
    "summary": "one sentence summary of what the member said",
    "language": "sw" or "en"
}

Examples:
- "Nitalipa kesho asubuhi" → payment_confirmed: true, intent: will_pay
- "Nimelipa tayari kupitia M-Pesa" → payment_confirmed: true, intent: already_paid
- "Sitaweza kulipa hadi mwisho wa mwezi" → payment_confirmed: false, intent: cannot_pay
- "Mkopo huu si wangu" → payment_confirmed: false, intent: dispute
"""


# ── Public API ────────────────────────────────────────────────────────────────

def build_reminder_message(reminder) -> str:
    """
    Generate a personalised reminder message in the member's preferred language.

    First tries a simple template (fast, no API cost).
    Falls back to Claude generation for complex or unusual loan situations.

    Args:
        reminder: A PaymentReminder model instance with .member and .loan loaded.

    Returns:
        str — text to be spoken via Africa's Talking TTS.
    """
    member = reminder.member
    loan   = reminder.loan
    lang   = member.preferred_language  # 'sw' or 'en'

    template = _TEMPLATES.get(lang, _TEMPLATES["en"]).get(
        reminder.reminder_type, _TEMPLATES["en"]["pre_due"]
    )

    due_date_str = loan.next_payment_date.strftime("%d %B %Y")

    try:
        message = template.format(
            name=member.first_name,
            amount=float(loan.outstanding_balance),
            installment=float(loan.monthly_installment),
            due_date=due_date_str,
        )
        return message
    except (KeyError, ValueError) as exc:
        logger.warning("Template formatting failed: %s — falling back to Claude", exc)
        return _generate_message_with_claude(reminder, lang)


def _generate_message_with_claude(reminder, lang: str) -> str:
    """
    Ask Claude to generate a personalised reminder message.
    Used as a fallback when template formatting fails.
    """
    member = reminder.member
    loan   = reminder.loan

    prompt = (
        f"Generate a polite, friendly loan payment reminder for a SACCO member. "
        f"Language: {'Kiswahili' if lang == 'sw' else 'English'}. "
        f"Member name: {member.first_name}. "
        f"Outstanding balance: KSh {loan.outstanding_balance:,.0f}. "
        f"Monthly installment: KSh {loan.monthly_installment:,.0f}. "
        f"Due date: {loan.next_payment_date.strftime('%d %B %Y')}. "
        f"Reminder type: {reminder.reminder_type}. "
        f"Keep it under 40 words, warm, and professional."
    )
    result = understand_with_claude(prompt)
    return result.get("response_text", _TEMPLATES["en"]["pre_due"].format(
        name=member.first_name,
        amount=float(loan.outstanding_balance),
        installment=float(loan.monthly_installment),
        due_date=loan.next_payment_date.strftime("%d %B %Y"),
    ))


@transaction.atomic
def initiate_reminder_call(reminder_id: int) -> bool:
    """
    Place an outbound call to the member using Africa's Talking Voice API.

    Flow
    ----
    1. Load reminder + related objects (select_for_update to prevent double-call)
    2. Build the spoken message
    3. Call AT Voice API
    4. Update reminder status + log the action

    Args:
        reminder_id: PK of the PaymentReminder to call.

    Returns:
        bool — True if the call was successfully queued with Africa's Talking.
    """
    from loans.models import PaymentReminder, ReminderLog
    from voice.models import CallLog

    try:
        reminder = (
            PaymentReminder.objects.select_related("member", "loan")
            .select_for_update()
            .get(pk=reminder_id)
        )
    except PaymentReminder.DoesNotExist:
        logger.error("initiate_reminder_call: reminder #%d not found", reminder_id)
        return False

    # Guard: don't double-call
    if reminder.status in ("calling", "sent", "completed", "cancelled"):
        logger.warning(
            "Reminder #%d already in status '%s' — skipping",
            reminder_id, reminder.status,
        )
        return False

    member = reminder.member
    message = build_reminder_message(reminder)

    # Build the TTS XML that Africa's Talking will play
    # The action URL receives the member's spoken response
    safe_message = (
        message
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    callback_url = getattr(settings, "AT_VOICE_REMINDER_RESPONSE_URL", "")

    # Africa's Talking expects the voice XML to be served from our action URL.
    # We pass the reminder_id as a query param so the response webhook knows
    # which reminder to update.
    action_url = f"{callback_url}?reminder_id={reminder_id}"

    try:
        voice = _get_at_voice()
        response = voice.call(
            callFrom=settings.AT_VOICE_NUMBER,    # e.g. "+254711082XXX"
            callTo=[member.phone_number],
            clientRequestId=str(reminder_id),
        )

        # AT returns a list of call results
        call_result = response.get("entries", [{}])[0]
        call_status = call_result.get("status", "").lower()
        call_sid    = call_result.get("sessionId", "")

        logger.info(
            "AT call placed for reminder #%d → member %s | status: %s | sid: %s",
            reminder_id, member.phone_number, call_status, call_sid,
        )

        # Update reminder
        reminder.status   = "sent"
        reminder.call_sid = call_sid
        reminder.attempts += 1
        reminder.save(update_fields=["status", "call_sid", "attempts"])

        # Log to CallLog (reuse existing voice app infrastructure)
        CallLog.objects.create(
            caller_number=member.phone_number,
            session_id=call_sid or f"reminder-{reminder_id}-{reminder.attempts}",
            status="received",
        )

        # Audit log
        ReminderLog.objects.create(
            reminder=reminder,
            action="call_initiated",
            notes=f"AT status: {call_status} | SID: {call_sid}",
        )
        return True

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Failed to initiate call for reminder #%d: %s",
            reminder_id, exc, exc_info=True,
        )
        reminder.status   = "failed"
        reminder.attempts += 1
        reminder.save(update_fields=["status", "attempts"])

        ReminderLog.objects.create(
            reminder=reminder,
            action="call_failed",
            notes=str(exc),
        )
        return False


@transaction.atomic
def handle_reminder_response(
    call_session_id: str,
    member_response: str,
    reminder_id: int | None = None,
) -> dict:
    """
    Process what the member said during the outbound reminder call.

    1. Find the matching PaymentReminder by call_sid or reminder_id.
    2. Send transcript to Claude to determine payment intent.
    3. Update reminder status and log the outcome.

    Args:
        call_session_id : Africa's Talking sessionId from the webhook.
        member_response : Transcribed speech from the member.
        reminder_id     : Optional reminder PK (passed as query param in action URL).

    Returns:
        dict — Claude analysis result with payment_confirmed, intent, summary.
    """
    from loans.models import PaymentReminder, ReminderLog

    # Locate the reminder
    reminder = None
    if reminder_id:
        reminder = PaymentReminder.objects.filter(pk=reminder_id).first()
    if not reminder and call_session_id:
        reminder = PaymentReminder.objects.filter(call_sid=call_session_id).first()

    if not reminder:
        logger.warning(
            "handle_reminder_response: no reminder found for sid=%s id=%s",
            call_session_id, reminder_id,
        )
        return {"payment_confirmed": False, "intent": "other", "summary": "Reminder not found"}

    # Send to Claude for intent understanding
    try:
        claude_result = understand_with_claude(member_response)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Claude error in handle_reminder_response: %s", exc, exc_info=True)
        claude_result = {"payment_confirmed": False, "intent": "other", "summary": str(exc)}

    payment_confirmed = bool(claude_result.get("payment_confirmed", False))
    intent   = claude_result.get("intent", "other")
    summary  = claude_result.get("summary", member_response[:200])

    # Persist
    reminder.response_received = member_response
    reminder.payment_confirmed = payment_confirmed
    reminder.status            = "completed"
    reminder.completed_at      = timezone.now()
    reminder.save(update_fields=[
        "response_received", "payment_confirmed", "status", "completed_at"
    ])

    action = "payment_confirmed" if payment_confirmed else "call_completed"
    ReminderLog.objects.create(
        reminder=reminder,
        action=action,
        notes=f"Intent: {intent} | Summary: {summary}",
    )

    logger.info(
        "Reminder #%d response processed: confirmed=%s intent=%s",
        reminder.pk, payment_confirmed, intent,
    )
    return {
        "payment_confirmed": payment_confirmed,
        "intent": intent,
        "summary": summary,
        "reminder_id": reminder.pk,
    }
