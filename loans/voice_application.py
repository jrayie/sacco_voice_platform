"""
loans/voice_application.py

Voice-driven loan application flow.

A member calls the SACCO line, says "Nataka mkopo" or "I want a loan",
and the system guides them through a natural bilingual conversation to
collect: amount, purpose, and preferred repayment period.

Flow summary
------------
1.  voice/views.py detects intent=apply_loan → calls start_loan_application()
2.  start_loan_application() creates a LoanApplication (or resumes one)
    and returns the first question as TTS text.
3.  For each subsequent voice turn, handle_application_response() is called.
    Claude extracts structured data; the function advances the conversation
    state and returns the next question or a summary.
4.  When all fields are collected, complete_application() runs eligibility
    checks, updates the application status, and returns the closing message.

All conversation state is stored on LoanApplication.conversation_state (JSON)
so that multi-turn calls work even if the HTTP process restarts between turns.

Public API
----------
start_loan_application(session_id, caller_number, language)  → str (TTS text)
handle_application_response(session_id, transcript)           → str (TTS text)
complete_application(session_id)                              → str (TTS text)
check_eligibility(member, amount_requested)                   → (bool, str, Decimal)
"""

import json
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from dateutil.relativedelta import relativedelta   # pip install python-dateutil

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from voice.claude_client import understand_with_claude

logger = logging.getLogger(__name__)

# ── Conversation stages ───────────────────────────────────────────────────────

STAGE_GREETING   = "greeting"      # just said "I want a loan"
STAGE_AMOUNT     = "ask_amount"    # waiting for amount
STAGE_PURPOSE    = "ask_purpose"   # waiting for purpose
STAGE_PERIOD     = "ask_period"    # waiting for repayment period
STAGE_CONFIRM    = "confirm"       # reading back summary, asking to confirm
STAGE_COMPLETE   = "complete"      # all done

# ── Script: prompts in Kiswahili and English ──────────────────────────────────

PROMPTS = {
    "sw": {
        STAGE_AMOUNT:  "Unataka kuomba mkopo wa kiasi gani?",
        STAGE_PURPOSE: "Asante. Unataka kutumia mkopo huu kwa nini?",
        STAGE_PERIOD:  "Sawa. Ungependa kulipa mkopo huu kwa miezi mingapi? Tunaoffer miezi 6, 12, au 24.",
        STAGE_CONFIRM: (
            "Niruhusu nikurudie. Unaomba mkopo wa KSh {amount:,.0f} "
            "kwa ajili ya {purpose}, unaolipwa kwa miezi {period}. "
            "Je, ni sahihi? Sema NDIO kukubali au HAPANA kubadilisha."
        ),
        "approved": (
            "Hongera {name}! Ombi lako la mkopo wa KSh {amount:,.0f} limeidhinishwa. "
            "Utaanza kulipa KSh {installment:,.0f} kila mwezi kuanzia {first_payment}. "
            "Fedha zitawasilishwa ndani ya siku 2 za kazi. Asante kwa kuamini SACCO yetu!"
        ),
        "pending": (
            "Asante {name}. Ombi lako la mkopo wa KSh {amount:,.0f} limepokelewa. "
            "Timu yetu itaangalia na kukujulisha ndani ya siku 1-2. "
            "Nambari yako ya ombi ni {app_id}. Asante."
        ),
        "rejected": (
            "Samahani {name}. Ombi lako la mkopo halikuidhinishwa kwa sasa. "
            "Sababu: {reason}. "
            "Tafadhali wasiliana na ofisi yetu kwa maelezo zaidi. Asante."
        ),
        "clarify_amount": (
            "Samahani, sikuelewa vizuri kiasi ulichotaja. "
            "Tafadhali sema kiasi kwa maneno wazi, kwa mfano: elfu ishirini au KSh 20,000."
        ),
        "clarify_period": (
            "Tafadhali sema muda wa kulipa mkopo — miezi 6, miezi 12, au miezi 24."
        ),
        "cancelled": "Ombi la mkopo limeghairiwa. Asante kwa kuwasiliana nasi.",
        "error": (
            "Samahani, kuna tatizo la kiufundi. "
            "Tafadhali jaribu tena au wasiliana na ofisi yetu."
        ),
    },
    "en": {
        STAGE_AMOUNT:  "How much would you like to borrow?",
        STAGE_PURPOSE: "Thank you. What will you use this loan for?",
        STAGE_PERIOD:  "How many months would you like to repay? We offer 6, 12, or 24 months.",
        STAGE_CONFIRM: (
            "Let me confirm your application. You are requesting KSh {amount:,.0f} "
            "for {purpose}, to be repaid over {period} months. "
            "Say YES to confirm or NO to change something."
        ),
        "approved": (
            "Congratulations {name}! Your loan application for KSh {amount:,.0f} "
            "has been approved. You will repay KSh {installment:,.0f} per month "
            "starting {first_payment}. Funds will be disbursed within 2 business days. "
            "Thank you for trusting your SACCO!"
        ),
        "pending": (
            "Thank you {name}. Your loan application for KSh {amount:,.0f} "
            "has been received and is under review. "
            "Our team will contact you within 1-2 business days. "
            "Your application number is {app_id}. Thank you."
        ),
        "rejected": (
            "We're sorry {name}. Your loan application could not be approved at this time. "
            "Reason: {reason}. "
            "Please contact our office for more details. Thank you."
        ),
        "clarify_amount": (
            "I didn't quite catch the amount you mentioned. "
            "Please say it clearly, for example: twenty thousand shillings or KSh 20,000."
        ),
        "clarify_period": (
            "Please say your preferred repayment period — 6 months, 12 months, or 24 months."
        ),
        "cancelled": "Your loan application has been cancelled. Thank you for calling.",
        "error": (
            "We're sorry, there is a technical issue. "
            "Please try again or contact our office."
        ),
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# ELIGIBILITY RULES
# ════════════════════════════════════════════════════════════════════════════════

# Monthly savings placeholder (KSh). Replace with real SACCO core system lookup.
_PLACEHOLDER_MONTHLY_SAVINGS = Decimal("10000.00")

# Supported repayment periods (months)
VALID_PERIODS = {6, 12, 24}
DEFAULT_PERIOD = 12

# Annual interest rate used for repayment plan calculation
DEFAULT_INTEREST_RATE = Decimal("12.00")   # % per annum


def check_eligibility(
    member,
    amount_requested: Decimal,
) -> tuple[bool, str, Decimal]:
    """
    Run simple rule-based eligibility checks.

    Rules
    -----
    1. Member must be active.
    2. Member must have fewer than 2 active loans.
    3. Requested amount must not exceed 3 × estimated monthly savings.

    Args:
        member           : Member model instance.
        amount_requested : Amount the member wants to borrow (KSh).

    Returns:
        (is_eligible, reason_text, max_eligible_amount)
    """
    from loans.models import Loan

    max_amount = _PLACEHOLDER_MONTHLY_SAVINGS * 3

    if not member.is_active:
        return False, "Member account is not active.", Decimal("0")

    active_loans = Loan.objects.filter(member=member, status="active").count()
    if active_loans >= 2:
        return (
            False,
            f"Member already has {active_loans} active loan(s). Maximum is 2.",
            Decimal("0"),
        )

    if amount_requested > max_amount:
        return (
            False,
            f"Requested amount KSh {amount_requested:,.0f} exceeds maximum "
            f"KSh {max_amount:,.0f} (3× monthly savings).",
            max_amount,
        )

    return True, "All eligibility checks passed.", max_amount


def _calculate_installment(principal: Decimal, months: int) -> Decimal:
    """
    Simple flat-rate monthly installment calculation.
    interest_total = principal × (rate/100)
    installment    = (principal + interest_total) / months
    """
    annual_rate    = DEFAULT_INTEREST_RATE / 100
    interest_total = principal * annual_rate
    installment    = (principal + interest_total) / months
    return installment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ════════════════════════════════════════════════════════════════════════════════
# CLAUDE EXTRACTION
# ════════════════════════════════════════════════════════════════════════════════

_EXTRACTION_SYSTEM_PROMPT = """You are processing a voice conversation for a Kenyan SACCO loan application.
Extract information from what the member said and respond ONLY with valid JSON.

Required format:
{
    "amount": null or number (KSh amount, e.g. 20000),
    "purpose": null or string (what the loan is for),
    "period_months": null or number (repayment period in months),
    "confirmed": null or boolean (did they say YES/NDIO to confirm?),
    "cancelled": false or boolean (did they want to cancel?),
    "language": "sw" or "en",
    "confidence": "high" or "low",
    "raw_amount_text": null or string (exactly what they said for amount),
    "notes": null or string (anything ambiguous worth noting)
}

Kiswahili number mappings:
  moja=1, mbili=2, tatu=3, nne=4, tano=5, sita=6, saba=7, nane=8, tisa=9, kumi=10,
  ishirini=20, thelathini=30, arobaini=40, hamsini=50, sitini=60, sabini=70,
  themanini=80, tisini=90, mia=100, elfu=1000, laki=100000, milioni=1000000

Examples:
  "elfu ishirini"        → amount: 20000
  "mia tano"             → amount: 500
  "laki moja"            → amount: 100000
  "kununua mbegu"        → purpose: "purchase maize seeds"
  "miezi kumi na mbili"  → period_months: 12
  "ndio"                 → confirmed: true
  "hapana"               → confirmed: false
  "ghairi" or "cancel"   → cancelled: true
"""


def _extract_with_claude(transcript: str, stage: str) -> dict:
    """
    Send transcript to Claude for structured extraction.
    Falls back to an empty dict on any error.
    """
    prompt = (
        f"Current conversation stage: {stage}\n"
        f"Member said: \"{transcript}\"\n"
        "Extract the relevant information for this stage."
    )
    try:
        # We reuse understand_with_claude but override the system prompt
        # by constructing the full payload directly via boto3.
        import boto3, json as _json
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "system": _EXTRACTION_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = client.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-20260514-v1:0",
            contentType="application/json",
            accept="application/json",
            body=_json.dumps(body),
        )
        raw = _json.loads(resp["body"].read())
        text = "".join(
            b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text"
        ).strip()
        return _json.loads(text)
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_extract_with_claude failed at stage %s: %s", stage, exc, exc_info=True)
        return {}


# ════════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════════════════════

def _get_or_create_application(session_id: str, caller_number: str, language: str):
    """
    Find an in-progress LoanApplication for this session or create a new one.
    Member lookup is by caller_number; new members get a placeholder record.
    """
    from loans.models import LoanApplication, Member

    # Try to find an existing in-progress application for this session
    existing = LoanApplication.objects.filter(
        session_id=session_id,
        status="pending_review",
    ).first()
    if existing:
        return existing, False

    # Look up member by phone number
    member = Member.objects.filter(phone_number=caller_number, is_active=True).first()

    if not member:
        # Non-registered caller — create a minimal placeholder member
        # In production, integrate with SACCO membership system
        member, _ = Member.objects.get_or_create(
            phone_number=caller_number,
            defaults={
                "first_name": "Caller",
                "last_name":  caller_number[-4:],   # last 4 digits as placeholder
                "preferred_language": language,
                "is_active": True,
            },
        )
        logger.warning(
            "Loan application from unregistered number %s — created placeholder member #%d",
            caller_number, member.pk,
        )

    app = LoanApplication.objects.create(
        member=member,
        amount_requested=Decimal("0"),   # will be filled during conversation
        session_id=session_id,
        caller_number=caller_number,
        conversation_state={
            "stage":    STAGE_AMOUNT,
            "language": language,
            "turns":    [],
        },
    )
    logger.info(
        "Created LoanApplication #%d for member %s (session %s)",
        app.pk, member.full_name, session_id,
    )
    return app, True


@transaction.atomic
def start_loan_application(
    session_id: str,
    caller_number: str,
    language: str = "sw",
) -> str:
    """
    Entry point called when a member expresses intent to apply for a loan.

    Args:
        session_id    : Africa's Talking session ID for the call.
        caller_number : Member's phone number (E.164).
        language      : "sw" or "en" detected by Claude earlier in the call.

    Returns:
        str — TTS text to speak to the member (first question).
    """
    try:
        app, created = _get_or_create_application(session_id, caller_number, language)
        lang     = language if language in PROMPTS else "sw"
        prompts  = PROMPTS[lang]
        question = prompts[STAGE_AMOUNT]

        if not created:
            # Resuming — return the question for the current stage
            state    = app.conversation_state
            stage    = state.get("stage", STAGE_AMOUNT)
            question = prompts.get(stage, prompts[STAGE_AMOUNT])

        logger.info(
            "start_loan_application: App #%d stage=%s lang=%s",
            app.pk, STAGE_AMOUNT, lang,
        )
        return question

    except Exception as exc:   # pylint: disable=broad-except
        logger.error("start_loan_application error: %s", exc, exc_info=True)
        return PROMPTS["sw"]["error"]


@transaction.atomic
def handle_application_response(session_id: str, transcript: str) -> str:
    """
    Process one turn of the voice application conversation.

    Finds the in-progress LoanApplication for this session, extracts
    structured data from the transcript using Claude, advances the
    conversation state, and returns the next prompt or summary.

    Args:
        session_id : Africa's Talking session ID.
        transcript : What the member said this turn (from STT).

    Returns:
        str — TTS text to speak next.
    """
    from loans.models import LoanApplication

    app = LoanApplication.objects.filter(
        session_id=session_id, status="pending_review"
    ).select_for_update().first()

    if not app:
        logger.warning("handle_application_response: no app for session %s", session_id)
        return PROMPTS["sw"]["error"]

    state  = app.conversation_state or {}
    stage  = state.get("stage", STAGE_AMOUNT)
    lang   = state.get("language", app.member.preferred_language or "sw")
    prompts = PROMPTS.get(lang, PROMPTS["sw"])

    # Append transcript to audit log
    turns = state.get("turns", [])
    turns.append({"stage": stage, "transcript": transcript})
    state["turns"] = turns

    # ── Extract structured info from this turn ────────────────────────────────
    extracted = _extract_with_claude(transcript, stage)
    logger.debug("Extracted from '%s' at stage %s: %s", transcript[:60], stage, extracted)

    # Check for cancellation at any point
    if extracted.get("cancelled"):
        app.status = "cancelled"
        app.conversation_state = state
        app.save(update_fields=["status", "conversation_state"])
        return prompts["cancelled"]

    # ── Advance state machine ─────────────────────────────────────────────────

    if stage == STAGE_AMOUNT:
        amount = extracted.get("amount")
        if not amount or float(amount) <= 0:
            # Could not parse amount — ask again
            app.conversation_state = state
            app.save(update_fields=["conversation_state"])
            return prompts["clarify_amount"]

        app.amount_requested = Decimal(str(amount))
        state["stage"] = STAGE_PURPOSE
        app.conversation_state = state
        app.save(update_fields=["amount_requested", "conversation_state"])
        return prompts[STAGE_PURPOSE]

    elif stage == STAGE_PURPOSE:
        purpose = extracted.get("purpose") or transcript.strip()
        app.purpose = purpose[:500]   # cap length
        state["stage"] = STAGE_PERIOD
        app.conversation_state = state
        app.save(update_fields=["purpose", "conversation_state"])
        return prompts[STAGE_PERIOD]

    elif stage == STAGE_PERIOD:
        period = extracted.get("period_months")
        # Map to nearest valid period
        if period:
            period = int(period)
            if period not in VALID_PERIODS:
                period = min(VALID_PERIODS, key=lambda p: abs(p - period))
        else:
            # Could not parse — ask again
            app.conversation_state = state
            app.save(update_fields=["conversation_state"])
            return prompts["clarify_period"]

        app.preferred_repayment_period = period
        state["stage"] = STAGE_CONFIRM
        app.conversation_state = state
        app.save(update_fields=["preferred_repayment_period", "conversation_state"])

        return prompts[STAGE_CONFIRM].format(
            amount=float(app.amount_requested),
            purpose=app.purpose,
            period=period,
        )

    elif stage == STAGE_CONFIRM:
        confirmed = extracted.get("confirmed")

        if confirmed is True:
            # Member confirmed — complete the application
            state["stage"] = STAGE_COMPLETE
            app.conversation_state = state
            # Capture full transcript
            app.transcript_original = "\n".join(
                f"[{t['stage']}] {t['transcript']}" for t in turns
            )
            app.claude_analysis = extracted
            app.save(update_fields=[
                "conversation_state", "transcript_original", "claude_analysis"
            ])
            return complete_application(session_id)

        elif confirmed is False:
            # Member wants to change something — restart amount question
            state["stage"] = STAGE_AMOUNT
            app.conversation_state = state
            app.save(update_fields=["conversation_state"])
            return prompts[STAGE_AMOUNT]

        else:
            # Ambiguous — repeat the confirmation prompt
            app.conversation_state = state
            app.save(update_fields=["conversation_state"])
            return prompts[STAGE_CONFIRM].format(
                amount=float(app.amount_requested),
                purpose=app.purpose,
                period=app.preferred_repayment_period,
            )

    else:
        # Already complete or unknown stage
        logger.warning("handle_application_response: unexpected stage '%s'", stage)
        return prompts["error"]


@transaction.atomic
def complete_application(session_id: str) -> str:
    """
    Finalise a loan application after all information is collected:
      1. Run eligibility check and persist EligibilityCheck.
      2. If eligible → auto-approve and generate RepaymentPlan.
      3. If not eligible → keep pending_review for human review.
      4. Notify SACCO manager.
      5. Return closing TTS message.

    Args:
        session_id : Africa's Talking session ID.

    Returns:
        str — Closing TTS message to speak to the member.
    """
    from loans.models import LoanApplication, EligibilityCheck, RepaymentPlan

    app = LoanApplication.objects.select_related("member").filter(
        session_id=session_id
    ).first()

    if not app:
        logger.error("complete_application: no app for session %s", session_id)
        return PROMPTS["sw"]["error"]

    member  = app.member
    lang    = app.conversation_state.get("language", member.preferred_language or "sw")
    prompts = PROMPTS.get(lang, PROMPTS["sw"])

    # ── Eligibility ───────────────────────────────────────────────────────────
    is_eligible, reason, max_amount = check_eligibility(member, app.amount_requested)

    EligibilityCheck.objects.update_or_create(
        loan_application=app,
        defaults={
            "member":             member,
            "is_eligible":        is_eligible,
            "max_eligible_amount": max_amount,
            "reasons":            reason,
        },
    )

    closing_msg = ""

    if is_eligible:
        # ── Auto-approve ──────────────────────────────────────────────────────
        app.approve(notes="Auto-approved by eligibility check.")

        # Generate repayment plan
        installment   = _calculate_installment(app.amount_requested, app.preferred_repayment_period)
        interest_rate = DEFAULT_INTEREST_RATE / 100
        interest_total = app.amount_requested * interest_rate
        total_repayment = app.amount_requested + interest_total
        first_payment   = date.today() + relativedelta(months=1)
        last_payment    = first_payment + relativedelta(months=app.preferred_repayment_period - 1)

        RepaymentPlan.objects.update_or_create(
            loan_application=app,
            defaults={
                "monthly_installment":  installment,
                "total_repayment":      total_repayment.quantize(Decimal("0.01")),
                "interest_total":       interest_total.quantize(Decimal("0.01")),
                "first_payment_date":   first_payment,
                "last_payment_date":    last_payment,
                "interest_rate_annual": DEFAULT_INTEREST_RATE,
            },
        )

        closing_msg = prompts["approved"].format(
            name=member.first_name,
            amount=float(app.amount_requested),
            installment=float(installment),
            first_payment=first_payment.strftime("%d %B %Y"),
        )
        logger.info("App #%d auto-approved for %s", app.pk, member.full_name)

    else:
        # ── Keep pending for human review ─────────────────────────────────────
        closing_msg = prompts["pending"].format(
            name=member.first_name,
            amount=float(app.amount_requested),
            app_id=app.pk,
        )
        logger.info(
            "App #%d pending review for %s — reason: %s",
            app.pk, member.full_name, reason,
        )

    # ── Notify manager ────────────────────────────────────────────────────────
    _notify_manager(app, is_eligible, reason)

    return closing_msg


def notify_rejection(app, reason: str) -> str:
    """
    Build and return a rejection TTS message. Called from admin views
    when a manager manually rejects an application.
    """
    lang    = app.member.preferred_language or "sw"
    prompts = PROMPTS.get(lang, PROMPTS["sw"])
    return prompts["rejected"].format(
        name=app.member.first_name, reason=reason
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _notify_manager(app, is_eligible: bool, reason: str):
    """Send an email summary to the SACCO manager."""
    manager_email = getattr(settings, "SACCO_MANAGER_EMAIL", None)
    if not manager_email:
        return

    status_word = "AUTO-APPROVED" if is_eligible else "PENDING REVIEW"
    subject = f"[SACCO] New Loan Application #{app.pk} — {status_word}"
    body = (
        f"New loan application received via voice.\n\n"
        f"Application #:  {app.pk}\n"
        f"Member:         {app.member.full_name} ({app.member.phone_number})\n"
        f"Amount:         KSh {app.amount_requested:,.0f}\n"
        f"Purpose:        {app.purpose}\n"
        f"Period:         {app.preferred_repayment_period} months\n"
        f"Status:         {status_word}\n"
        f"Reason:         {reason}\n"
        f"Applied at:     {app.application_date.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Review at: https://your-domain.com/loans/applications/{app.pk}/\n"
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@sacco.co.ke"),
            recipient_list=[manager_email],
            fail_silently=True,
        )
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_notify_manager email failed: %s", exc)
