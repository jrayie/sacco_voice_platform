"""
loans/views.py

Staff-only views for managing loan reminders and applications.
"""

import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Reminders ─────────────────────────────────────────────────────────────────

@staff_member_required
def reminders_list(request):
    """Paginated, filterable list of PaymentReminder records."""
    from loans.models import PaymentReminder

    qs = PaymentReminder.objects.select_related("member", "loan").order_by("-created_at")

    status_filter = request.GET.get("status", "")
    type_filter   = request.GET.get("type", "")
    if status_filter:
        qs = qs.filter(status=status_filter)
    if type_filter:
        qs = qs.filter(reminder_type=type_filter)

    page = Paginator(qs, 25).get_page(request.GET.get("page", 1))

    return render(request, "loans/reminders_list.html", {
        "page_obj":      page,
        "status_filter": status_filter,
        "type_filter":   type_filter,
        "status_choices": PaymentReminder.STATUS_CHOICES,
        "type_choices":   PaymentReminder.REMINDER_TYPE_CHOICES,
        "total":          qs.count(),
    })


# ── Loan Applications ─────────────────────────────────────────────────────────

@staff_member_required
def applications_list(request):
    """
    Paginated list of LoanApplication records with status filter.
    """
    from loans.models import LoanApplication

    qs = (
        LoanApplication.objects
        .select_related("member", "reviewed_by")
        .order_by("-application_date")
    )

    status_filter = request.GET.get("status", "")
    if status_filter:
        qs = qs.filter(status=status_filter)

    page = Paginator(qs, 20).get_page(request.GET.get("page", 1))

    return render(request, "loans/applications_list.html", {
        "page_obj":      page,
        "status_filter": status_filter,
        "status_choices": LoanApplication.APPLICATION_STATUS_CHOICES,
        "total":          qs.count(),
    })


@staff_member_required
def application_detail(request, app_id):
    """
    Full detail view for a single LoanApplication.
    Includes transcript, Claude analysis, eligibility check, and repayment plan.
    """
    from loans.models import LoanApplication

    app = get_object_or_404(
        LoanApplication.objects.select_related(
            "member", "reviewed_by"
        ).prefetch_related(
            "eligibility", "repayment_plan"
        ),
        pk=app_id,
    )
    return render(request, "loans/application_detail.html", {"app": app})


@staff_member_required
def approve_application(request, app_id):
    """
    POST endpoint to approve a pending loan application.
    Redirects back to the detail page with a success message.
    """
    if request.method != "POST":
        return redirect("loans:application_detail", app_id=app_id)

    from loans.models import LoanApplication, RepaymentPlan
    from loans.voice_application import _calculate_installment, DEFAULT_INTEREST_RATE
    from decimal import Decimal
    from datetime import date
    from dateutil.relativedelta import relativedelta

    app = get_object_or_404(LoanApplication, pk=app_id)

    if app.status != "pending_review":
        messages.warning(request, f"Application #{app_id} is already {app.status}.")
        return redirect("loans:application_detail", app_id=app_id)

    notes = request.POST.get("notes", "Approved by SACCO manager.")
    app.approve(reviewed_by=request.user, notes=notes)

    # Generate repayment plan if it doesn't exist
    if not RepaymentPlan.objects.filter(loan_application=app).exists():
        installment    = _calculate_installment(app.amount_requested, app.preferred_repayment_period)
        interest_rate  = DEFAULT_INTEREST_RATE / 100
        interest_total = app.amount_requested * interest_rate
        total_repay    = (app.amount_requested + interest_total).quantize(Decimal("0.01"))
        first_payment  = date.today() + relativedelta(months=1)
        last_payment   = first_payment + relativedelta(months=app.preferred_repayment_period - 1)

        RepaymentPlan.objects.create(
            loan_application=app,
            monthly_installment=installment,
            total_repayment=total_repay,
            interest_total=interest_total.quantize(Decimal("0.01")),
            first_payment_date=first_payment,
            last_payment_date=last_payment,
            interest_rate_annual=DEFAULT_INTEREST_RATE,
        )

    logger.info("App #%d approved by %s", app_id, request.user.username)
    messages.success(
        request,
        f"Application #{app_id} approved. KSh {app.amount_requested:,.0f} "
        f"for {app.member.full_name}.",
    )
    return redirect("loans:application_detail", app_id=app_id)


@staff_member_required
def reject_application(request, app_id):
    """
    POST endpoint to reject a pending loan application.
    """
    if request.method != "POST":
        return redirect("loans:application_detail", app_id=app_id)

    from loans.models import LoanApplication

    app = get_object_or_404(LoanApplication, pk=app_id)

    if app.status != "pending_review":
        messages.warning(request, f"Application #{app_id} is already {app.status}.")
        return redirect("loans:application_detail", app_id=app_id)

    notes = request.POST.get("notes", "Rejected by SACCO manager.")
    app.reject(reviewed_by=request.user, notes=notes)

    logger.info("App #%d rejected by %s: %s", app_id, request.user.username, notes)
    messages.warning(
        request,
        f"Application #{app_id} rejected ({app.member.full_name}).",
    )
    return redirect("loans:application_detail", app_id=app_id)
