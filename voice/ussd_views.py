"""
voice/ussd_views.py

Africa's Talking USSD callback handler for the SACCO Voice Platform.

Connects to existing loans app models (Member, Loan, LoanApplication) so
members can check balances, apply for loans, and track applications entirely
over USSD — no smartphone required.

Protocol
--------
AT sends a POST to /api/voice/ussd/ with:
    sessionId   — unique per USSD session
    serviceCode — e.g. *384*47998#
    phoneNumber — caller's E.164 number
    text        — all inputs joined by *, e.g. "1*2*50000"

We respond with plain text:
    CON <message>  → session continues (menu shown)
    END <message>  → session ends (final message)

Flow example
------------
User dials *384*47998#
  → text=""          → main menu (CON)
User presses 2 (Loan Balance)
  → text="2"         → loan balance sub-menu (CON)
User presses 1 (Check Loan Balance)
  → text="2*1"       → actual balance (END)
"""

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

# ── Response helpers ──────────────────────────────────────────────────────────

def _con(text: str) -> HttpResponse:
    """Continue session — AT shows the text and waits for more input."""
    return HttpResponse(f"CON {text}", content_type="text/plain")


def _end(text: str) -> HttpResponse:
    """End session — AT shows the text and closes the USSD session."""
    return HttpResponse(f"END {text}", content_type="text/plain")


# ── Menu strings ──────────────────────────────────────────────────────────────

def _main_menu() -> str:
    return (
        "Karibu SACCO Voice Platform\n"
        "1. Check Balance\n"
        "2. Check Loan Balance\n"
        "3. Apply for Loan\n"
        "4. Speak to Agent\n"
        "5. Help"
    )


def _loan_balance_menu() -> str:
    return (
        "Loan Balance\n"
        "1. Check Loan Balance\n"
        "2. Loan Payment Schedule\n"
        "0. Back to Main Menu"
    )


def _apply_loan_menu() -> str:
    return (
        "Apply for Loan\n"
        "1. Apply New Loan\n"
        "2. Check Application Status\n"
        "0. Back to Main Menu"
    )


def _help_menu() -> str:
    return (
        "Help & Support\n"
        "1. Voice Support (Call us)\n"
        "2. FAQs\n"
        "0. Back to Main Menu"
    )


# ── Member helpers ────────────────────────────────────────────────────────────

def _get_or_create_member(phone_number: str):
    """
    Return an existing Member for this phone number, or create a minimal
    placeholder record for first-time USSD users.

    The placeholder uses the last 4 digits of the phone number as the
    last_name so staff can identify unregistered callers in the admin.

    Args:
        phone_number: E.164 format, e.g. +254712345678

    Returns:
        (member, created) tuple — same contract as get_or_create().
    """
    from loans.models import Member
    return Member.objects.get_or_create(
        phone_number=phone_number,
        defaults={
            "first_name": "USSD",
            "last_name":  f"User-{phone_number[-4:]}",
            "preferred_language": "sw",
            "is_active": True,
        },
    )


# ── Business logic ────────────────────────────────────────────────────────────

def _show_balance(phone_number: str) -> HttpResponse:
    """
    Show the member's savings/account balance.

    NOTE: The Member model does not yet have an account_balance field.
    This is a placeholder that returns a realistic message while integration
    with the SACCO core system is pending. When account_balance is added to
    Member, replace the placeholder line with:
        balance = member.account_balance
    """
    try:
        member, created = _get_or_create_member(phone_number)
        if created:
            return _end(
                "Samahani, akaunti yako haijapatikana.\n"
                "Tafadhali wasiliana na ofisi yetu.\n"
                "Sorry, your account was not found.\n"
                "Please contact our office."
            )
        # Placeholder — replace with real core-banking lookup
        balance = getattr(member, "account_balance", None)
        if balance is not None:
            return _end(
                f"Akaunti: {member.full_name}\n"
                f"Salio: KSh {balance:,.2f}\n\n"
                f"Account: {member.full_name}\n"
                f"Balance: KSh {balance:,.2f}"
            )
        return _end(
            f"Habari {member.first_name},\n"
            "Salio halionekani kwa sasa. Tafadhali piga simu ofisi yetu.\n\n"
            f"Hello {member.first_name},\n"
            "Balance unavailable. Please call our office for details."
        )
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_show_balance error for %s: %s", phone_number, exc, exc_info=True)
        return _end("Hitilafu imetokea. Tafadhali jaribu tena.\nError occurred. Please try again.")


def _show_loan_balance(phone_number: str) -> HttpResponse:
    """
    Show all active loans and their outstanding balances.
    Lists up to 3 loans to keep the USSD message within the 182-character limit.
    """
    try:
        from loans.models import Loan
        member, created = _get_or_create_member(phone_number)
        if created:
            return _end(
                "Huna mkopo wowote. / You have no active loans.\n"
                "Chagua 'Apply for Loan' kuomba mkopo."
            )
        active_loans = Loan.objects.filter(
            member=member, status="active"
        ).order_by("next_payment_date")[:3]

        if not active_loans.exists():
            return _end(
                f"Habari {member.first_name},\n"
                "Huna mkopo wowote kwa sasa.\n"
                "You have no active loans."
            )
        lines = [f"Habari {member.first_name}, Mikopo yako:"]
        total = Decimal("0")
        for i, loan in enumerate(active_loans, 1):
            lines.append(
                f"{i}. KSh {loan.outstanding_balance:,.0f} "
                f"(Due {loan.next_payment_date.strftime('%d %b')})"
            )
            total += loan.outstanding_balance
        lines.append(f"Jumla/Total: KSh {total:,.0f}")
        return _end("\n".join(lines))
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_show_loan_balance error for %s: %s", phone_number, exc, exc_info=True)
        return _end("Hitilafu imetokea. Please try again.")


def _show_loan_schedule(phone_number: str) -> HttpResponse:
    """
    Show the next 3 upcoming payment dates and amounts for active loans.
    Keeps the message short enough to fit within USSD character limits.
    """
    try:
        from loans.models import Loan
        member, created = _get_or_create_member(phone_number)
        if created:
            return _end("Account not found. Please contact our office.")

        active_loans = Loan.objects.filter(
            member=member, status="active"
        ).order_by("next_payment_date")[:3]

        if not active_loans.exists():
            return _end(
                f"Habari {member.first_name},\n"
                "Huna mkopo wowote wa kulipa.\n"
                "You have no active loans."
            )
        lines = ["Ratiba ya Malipo / Payment Schedule:"]
        for loan in active_loans:
            lines.append(
                f"KSh {loan.monthly_installment:,.0f} "
                f"due {loan.next_payment_date.strftime('%d %b %Y')}"
            )
        lines.append("\nLipa kupitia M-Pesa au ofisi yetu.")
        lines.append("Pay via M-Pesa or our offices.")
        return _end("\n".join(lines))
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_show_loan_schedule error for %s: %s", phone_number, exc, exc_info=True)
        return _end("Hitilafu imetokea. Please try again.")


def _check_application_status(phone_number: str) -> HttpResponse:
    """
    Show the status of the member's most recent loan application(s).
    Lists up to 3 most recent applications.
    """
    try:
        from loans.models import LoanApplication
        member, created = _get_or_create_member(phone_number)
        if created:
            return _end(
                "Huna maombi ya mkopo.\n"
                "You have no loan applications.\n"
                "Select 'Apply for Loan' to apply."
            )
        apps = LoanApplication.objects.filter(
            member=member
        ).order_by("-application_date")[:3]

        if not apps.exists():
            return _end(
                f"Habari {member.first_name},\n"
                "Huna maombi ya mkopo.\n"
                "You have no loan applications yet."
            )
        status_labels = {
            "pending_review": "Inasubiri / Pending",
            "approved":       "Imeidhinishwa / Approved",
            "rejected":       "Imekataliwa / Rejected",
            "disbursed":      "Imetumwa / Disbursed",
            "cancelled":      "Imeghairiwa / Cancelled",
        }
        lines = ["Maombi yako ya Mkopo:"]
        for app in apps:
            label = status_labels.get(app.status, app.status.title())
            lines.append(
                f"#{app.pk} KSh {app.amount_requested:,.0f} — {label}"
            )
        return _end("\n".join(lines))
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_check_application_status error for %s: %s",
                     phone_number, exc, exc_info=True)
        return _end("Hitilafu imetokea. Please try again.")


@transaction.atomic
def _submit_loan_application(
    phone_number: str,
    amount_str: str,
    session_id: str,
) -> HttpResponse:
    """
    Validate the amount entered by the member and create a LoanApplication.

    Args:
        phone_number : Member's E.164 phone number.
        amount_str   : Raw string the member typed (may contain commas/spaces).
        session_id   : USSD session ID — stored as the application's session_id.

    Returns:
        HttpResponse with END prefix confirming or rejecting the application.
    """
    from loans.models import LoanApplication
    from loans.voice_application import check_eligibility

    # ── Parse amount ──────────────────────────────────────────────────────────
    cleaned = amount_str.strip().replace(",", "").replace(" ", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return _end(
            "Kiasi ulichoweka si sahihi.\n"
            f"'{amount_str}' si nambari halali.\n"
            "Jaribu tena na nambari kama 10000."
        )

    if amount <= 0:
        return _end(
            "Kiasi lazima iwe zaidi ya sifuri.\n"
            "Amount must be greater than zero."
        )
    if amount > Decimal("5000000"):
        return _end(
            "Kiasi ni kikubwa mno. Kiwango cha juu ni KSh 5,000,000.\n"
            "Amount too large. Maximum is KSh 5,000,000."
        )

    # ── Get or create member ──────────────────────────────────────────────────
    try:
        member, _ = _get_or_create_member(phone_number)
    except Exception as exc:   # pylint: disable=broad-except
        logger.error("_submit_loan_application: member lookup failed: %s", exc,
                     exc_info=True)
        return _end("Hitilafu imetokea. Tafadhali jaribu tena.")

    # ── Quick eligibility check ───────────────────────────────────────────────
    is_eligible, reason, _ = check_eligibility(member, amount)

    if not is_eligible:
        return _end(
            f"Samahani, ombi lako haliwezekani kwa sasa.\n"
            f"Sababu: {reason}\n\n"
            f"Sorry, your application cannot proceed.\n"
            f"Reason: {reason}"
        )

    # ── Create LoanApplication ────────────────────────────────────────────────
    try:
        app = LoanApplication.objects.create(
            member=member,
            amount_requested=amount,
            purpose="USSD application",
            preferred_repayment_period=12,          # default; member can change via branch
            status="pending_review",
            session_id=f"ussd-{session_id}",
            caller_number=phone_number,
            transcript_original=(
                f"USSD application via {phone_number} on "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. "
                f"Amount: KSh {amount:,.0f}"
            ),
            conversation_state={"stage": "complete", "channel": "ussd"},
        )
        logger.info(
            "USSD LoanApplication #%d created for %s — KSh %s",
            app.pk, phone_number, amount,
        )
        return _end(
            f"Asante {member.first_name}!\n"
            f"Ombi la mkopo wa KSh {amount:,.0f} limepokelewa.\n"
            f"Nambari ya ombi: #{app.pk}\n"
            f"Tutawasiliana nawe ndani ya siku 1-2.\n\n"
            f"Thank you {member.first_name}!\n"
            f"Loan application for KSh {amount:,.0f} received.\n"
            f"Application number: #{app.pk}\n"
            f"We will contact you within 1-2 business days."
        )
    except Exception as exc:   # pylint: disable=broad-except
        logger.error(
            "_submit_loan_application: DB error for %s KSh %s: %s",
            phone_number, amount, exc, exc_info=True,
        )
        return _end(
            "Ombi halikuweza kuhifadhiwa. Tafadhali jaribu tena.\n"
            "Application could not be saved. Please try again."
        )


def _speak_to_agent() -> HttpResponse:
    """Return the SACCO agent contact number."""
    return _end(
        "Piga simu wakala wetu:\n"
        "Call our agent:\n"
        "0800 720 XXX (Free)\n"
        "Mon-Fri 8am-5pm\n"
        "Sat 9am-1pm"
    )


def _voice_support() -> HttpResponse:
    """Return voice support / callback details."""
    return _end(
        "Msaada wa Sauti / Voice Support:\n"
        "Piga: 0800 720 XXX (Bure)\n"
        "Call: 0800 720 XXX (Free)\n"
        "Au piga simu *384*47998# tena.\n"
        "Or dial *384*47998# again.\n"
        "Wakati: Jumatatu-Ijumaa 8am-5pm."
    )


def _faqs() -> HttpResponse:
    """Return a short FAQ list."""
    return _end(
        "Maswali Yanayoulizwa Mara Kwa Mara:\n"
        "1. Mkopo: Piga *384*47998# > Omba Mkopo\n"
        "2. Malipo: M-Pesa Paybill 123456\n"
        "3. Salio: Piga *384*47998# > Angalia Salio\n"
        "4. Ofisi: 0800 720 XXX\n\n"
        "FAQs:\n"
        "1. Loans: Dial *384*47998# > Apply\n"
        "2. Payments: M-Pesa Paybill 123456\n"
        "3. Balance: Dial *384*47998# > Balance\n"
        "4. Office: 0800 720 XXX"
    )


# ── Session text parser ───────────────────────────────────────────────────────

def _parse_text(text: str) -> list[str]:
    """
    Split the AT USSD `text` field into a list of individual inputs.

    AT concatenates all inputs with '*':
        ""        → []           (session just started)
        "1"       → ["1"]        (pressed 1 on main menu)
        "2*1"     → ["2", "1"]   (pressed 2 then 1)
        "3*1*50000" → ["3", "1", "50000"]
    """
    if not text:
        return []
    return [part.strip() for part in text.split("*") if part.strip()]


# ── Menu handlers ─────────────────────────────────────────────────────────────

def _handle_main_menu(phone_number: str, option: str) -> HttpResponse:
    """
    Route a top-level menu selection to the appropriate sub-menu or action.

    Args:
        phone_number : Member's E.164 phone number.
        option       : The digit the member pressed ("1"–"5").
    """
    if option == "1":
        return _show_balance(phone_number)
    if option == "2":
        return _con(_loan_balance_menu())
    if option == "3":
        return _con(_apply_loan_menu())
    if option == "4":
        return _speak_to_agent()
    if option == "5":
        return _con(_help_menu())
    return _con(
        "Chaguo si sahihi. Tafadhali chagua tena.\n"
        "Invalid option. Please choose again.\n\n"
        + _main_menu()
    )


def _handle_loan_balance_submenu(
    phone_number: str, option: str
) -> HttpResponse:
    """Handle Loan Balance sub-menu (depth=2, first input was '2')."""
    if option == "1":
        return _show_loan_balance(phone_number)
    if option == "2":
        return _show_loan_schedule(phone_number)
    if option == "0":
        return _con(_main_menu())
    return _con(
        "Chaguo si sahihi.\n\n" + _loan_balance_menu()
    )


def _handle_apply_loan_submenu(
    phone_number: str, option: str
) -> HttpResponse:
    """
    Handle Apply for Loan sub-menu (depth=2, first input was '3').

    option "1" → ask for the loan amount (returns CON to keep session open)
    option "2" → check application status
    option "0" → back to main menu
    """
    if option == "1":
        return _con(
            "Ingiza kiasi cha mkopo kwa KSh\n"
            "(Mfano: 10000 au 50000)\n\n"
            "Enter loan amount in KSh\n"
            "(Example: 10000 or 50000)\n\n"
            "0. Rudi / Back to menu"
        )
    if option == "2":
        return _check_application_status(phone_number)
    if option == "0":
        return _con(_main_menu())
    return _con(
        "Chaguo si sahihi.\n\n" + _apply_loan_menu()
    )


def _handle_help_submenu(phone_number: str, option: str) -> HttpResponse:
    """Handle Help sub-menu (depth=2, first input was '5')."""
    if option == "1":
        return _voice_support()
    if option == "2":
        return _faqs()
    if option == "0":
        return _con(_main_menu())
    return _con(
        "Chaguo si sahihi.\n\n" + _help_menu()
    )


# ════════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════

@csrf_exempt
def ussd_callback(request):
    """
    Africa's Talking USSD callback — main entry point.

    AT POSTs here on every keypress within the USSD session.
    We inspect the accumulated `text` field to determine the current
    position in the menu tree and return the appropriate response.

    Expected POST params
    --------------------
    sessionId   : Unique per USSD session (reused across keypresses)
    serviceCode : The USSD code, e.g. *384*47998#
    phoneNumber : Caller's number in E.164 format
    text        : All inputs so far joined by *, e.g. "2*1"

    Response format
    ---------------
    Plain text starting with "CON " or "END ".
    Content-Type must be text/plain.

    Menu tree (inputs → screen)
    ---------------------------
    ""        → main menu
    "1"       → check balance (END)
    "2"       → loan balance sub-menu
    "2*1"     → show loan balance (END)
    "2*2"     → show loan schedule (END)
    "2*0"     → main menu
    "3"       → apply loan sub-menu
    "3*1"     → enter amount prompt
    "3*1*0"   → back to main menu
    "3*1*<n>" → submit application with amount n (END)
    "3*2"     → check application status (END)
    "3*0"     → main menu
    "4"       → speak to agent (END)
    "5"       → help sub-menu
    "5*1"     → voice support (END)
    "5*2"     → FAQs (END)
    "5*0"     → main menu
    """
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    # ── Parse AT parameters ───────────────────────────────────────────────────
    session_id   = request.POST.get("sessionId", "")
    service_code = request.POST.get("serviceCode", "")
    phone_number = request.POST.get("phoneNumber", "")
    text         = request.POST.get("text", "")

    inputs = _parse_text(text)
    depth  = len(inputs)

    logger.info(
        "USSD session=%s phone=%s code=%s text=%r depth=%d",
        session_id, phone_number, service_code, text, depth,
    )

    # ── Guard: phone number required ──────────────────────────────────────────
    if not phone_number:
        logger.error("USSD request missing phoneNumber — session %s", session_id)
        return _end(
            "Hitilafu: nambari ya simu haikupatikana.\n"
            "Error: phone number not received."
        )

    try:
        # ── Depth 0: session just started → show main menu ────────────────────
        if depth == 0:
            return _con(_main_menu())

        main_option = inputs[0]

        # ── Depth 1: user chose from main menu ────────────────────────────────
        if depth == 1:
            return _handle_main_menu(phone_number, main_option)

        # ── Depth 2: user chose from a sub-menu ──────────────────────────────
        if depth == 2:
            sub_option = inputs[1]

            if main_option == "2":
                return _handle_loan_balance_submenu(phone_number, sub_option)

            if main_option == "3":
                return _handle_apply_loan_submenu(phone_number, sub_option)

            if main_option == "5":
                return _handle_help_submenu(phone_number, sub_option)

            # Any other depth-2 case — show main menu again
            return _con(_main_menu())

        # ── Depth 3: data entry after a sub-menu prompt ───────────────────────
        if depth == 3:
            sub_option   = inputs[1]
            data_input   = inputs[2]

            # Apply New Loan → user entered amount
            if main_option == "3" and sub_option == "1":
                if data_input == "0":
                    return _con(_main_menu())
                return _submit_loan_application(
                    phone_number, data_input, session_id
                )

            # Any unrecognised depth-3 path
            logger.warning(
                "Unhandled depth-3 path: %r (session %s)", text, session_id
            )
            return _con(_main_menu())

        # ── Depth > 3: shouldn't happen; reset to main menu ───────────────────
        logger.warning(
            "USSD depth %d exceeded max (session %s text=%r)",
            depth, session_id, text,
        )
        return _con(
            "Tafadhali anza upya.\nPlease start again.\n\n" + _main_menu()
        )

    except Exception as exc:   # pylint: disable=broad-except
        logger.error(
            "Unhandled USSD error — session=%s phone=%s text=%r: %s",
            session_id, phone_number, text, exc, exc_info=True,
        )
        return _end(
            "Hitilafu imetokea. Tafadhali jaribu tena.\n"
            "An error occurred. Please try again.\n"
            "Msaada: 0800 720 XXX"
        )
