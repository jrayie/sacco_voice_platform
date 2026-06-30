"""
analytics/views.py

Dashboard views for SACCO managers.

Views
-----
dashboard_home     — Main KPI dashboard with charts, recent calls, and reminder widgets
call_detail        — Individual call record with transcript and recording
api_call_stats     — JSON endpoint powering the AJAX chart refresh
api_call_detail    — JSON endpoint for call detail modal
export_report      — Download call logs as CSV
api_reminder_stats — JSON endpoint for reminder chart widget
"""

import json
import logging
from datetime import date, timedelta, datetime

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.db.models import Avg, Count, Q
from django.views.decorators.http import require_GET
from django.contrib.auth.decorators import login_required

from voice.models import CallLog

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _intent_display(intent: str) -> str:
    return intent.replace("_", " ").title() if intent else "Unknown"


def _last_7_days():
    """Return start and end dates for the last 7 days."""
    end = date.today()
    start = end - timedelta(days=6)
    return start, end


def _last_30_days():
    """Return start and end dates for the last 30 days."""
    end = date.today()
    start = end - timedelta(days=29)
    return start, end


def _date_range(start, end):
    """Generate a list of dates from start to end inclusive."""
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def _get_reminder_summary(today: date) -> dict:
    """
    Return reminder KPIs for dashboard cards and the status distribution chart.
    Returns zeros gracefully if the loans app is not installed.
    """
    try:
        from loans.models import PaymentReminder

        today_qs = PaymentReminder.objects.filter(reminder_date=today)
        total_today = today_qs.count()
        sent_today = today_qs.filter(status__in=["sent", "completed"]).count()
        confirmed = today_qs.filter(payment_confirmed=True).count()
        confirmation_rate = round(confirmed / sent_today * 100, 1) if sent_today else 0

        # Status distribution for the donut chart (last 7 days)
        week_start, _ = _last_7_days()
        status_dist = (
            PaymentReminder.objects
            .filter(reminder_date__gte=week_start)
            .values("status")
            .annotate(cnt=Count("id"))
            .order_by("status")
        )
        status_chart = {
            "labels": [r["status"].title() for r in status_dist],
            "counts": [r["cnt"] for r in status_dist],
        }

        # Recent reminders table
        recent_reminders = (
            PaymentReminder.objects
            .select_related("member", "loan")
            .order_by("-created_at")[:8]
        )

        return {
            "reminders_today": total_today,
            "confirmation_rate": confirmation_rate,
            "status_chart_json": json.dumps(status_chart),
            "recent_reminders": recent_reminders,
            "loans_app_available": True,
        }
    except Exception as exc:
        logger.debug("loans app not available: %s", exc)
        return {
            "reminders_today": 0,
            "confirmation_rate": 0,
            "status_chart_json": json.dumps({"labels": [], "counts": []}),
            "recent_reminders": [],
            "loans_app_available": False,
        }


# ── Main Dashboard ──────────────────────────────────────────────────────────

@login_required
def dashboard_home(request):
    """
    Main analytics dashboard.

    Shows:
    - Call KPI cards (today)
    - Reminder KPI cards (today)
    - Line charts: calls/day + accuracy trend (7 days)
    - Bar chart: top 5 intents this week
    - Donut chart: reminder status distribution
    - Recent calls table
    - Recent reminders table
    """
    today = date.today()
    cache_key = f"dashboard_home_{today}"
    ctx = cache.get(cache_key)

    if ctx is None:
        # ── Call summary cards ────────────────────────────────────────────────
        today_calls = CallLog.objects.filter(created_at__date=today)
        today_total = today_calls.count()
        today_ai = today_calls.exclude(intent_detected="speak_to_agent").filter(
            status="completed"
        ).count()
        today_ai_pct = round(today_ai / today_total * 100, 1) if today_total else 0
        today_avg_dur = today_calls.aggregate(avg=Avg("duration_seconds"))["avg"] or 0
        today_escalated = today_calls.filter(intent_detected="speak_to_agent").count()
        today_esc_pct = round(today_escalated / today_total * 100, 1) if today_total else 0

        # ── 7-day trend charts ────────────────────────────────────────────────
        week_start, week_end = _last_7_days()
        
        # Get metrics for each day
        dates = []
        total_calls = []
        accuracy = []
        
        for d in _date_range(week_start, week_end):
            day_calls = CallLog.objects.filter(created_at__date=d)
            day_total = day_calls.count()
            day_ai = day_calls.exclude(intent_detected="speak_to_agent").filter(
                status="completed"
            ).count()
            
            dates.append(str(d))
            total_calls.append(day_total)
            accuracy.append(round(day_ai / day_total * 100, 1) if day_total else 0)
        
        chart_data = {
            "dates": dates,
            "total_calls": total_calls,
            "accuracy": accuracy,
        }

        # ── Top 5 intents this week ───────────────────────────────────────────
        wk_start, wk_end = _last_7_days()
        top_intents_qs = (
            CallLog.objects
            .filter(created_at__date__range=(wk_start, wk_end))
            .exclude(intent_detected="")
            .values("intent_detected")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:5]
        )
        top_intents = [
            {"intent": _intent_display(r["intent_detected"]), "count": r["cnt"]}
            for r in top_intents_qs
        ]

        # ── Calls by hour today (SQLite-compatible) ──────────────────────────
        hourly = {}
        for hour in range(24):
            # Filter by hour using datetime range
            start_time = datetime.combine(today, datetime.min.time().replace(hour=hour))
            end_time = start_time + timedelta(hours=1)
            count = CallLog.objects.filter(
                created_at__gte=start_time,
                created_at__lt=end_time
            ).count()
            hourly[hour] = count
        
        hourly_data = {
            "hours": list(range(24)),
            "counts": [hourly.get(h, 0) for h in range(24)],
        }

        # ── Recent calls ──────────────────────────────────────────────────────
        recent_calls = CallLog.objects.order_by("-created_at")[:10]

        # ── Reminder widgets ──────────────────────────────────────────────────
        reminder_ctx = _get_reminder_summary(today)

        # ── Loan application widgets ──────────────────────────────────────────
        application_ctx = _get_application_summary(today)

        ctx = {
            # Call cards
            "today_total": today_total,
            "today_ai_pct": today_ai_pct,
            "today_avg_dur": round(today_avg_dur, 0),
            "today_esc_pct": today_esc_pct,
            # Charts
            "chart_data_json": json.dumps(chart_data),
            "top_intents_json": json.dumps(top_intents),
            "hourly_data_json": json.dumps(hourly_data),
            # Tables
            "recent_calls": recent_calls,
            "today": today,
            **reminder_ctx,
            **application_ctx,
        }
        cache.set(cache_key, ctx, CACHE_TTL)

    return render(request, "analytics/dashboard_home.html", ctx)


# ── Call Detail ─────────────────────────────────────────────────────────────

@login_required
def call_detail(request, call_id):
    """Individual call record — transcript, intent, response, recording player."""
    call = get_object_or_404(CallLog, pk=call_id)
    return render(request, "analytics/call_detail.html", {"call": call})


# ── JSON APIs ──────────────────────────────────────────────────────────────

@login_required
@require_GET
def api_call_stats(request):
    """
    JSON chart data endpoint (AJAX).

    Query params
    ------------
    range : "7d" (default) | "30d"
    """
    range_param = request.GET.get("range", "7d")
    start, end = _last_30_days() if range_param == "30d" else _last_7_days()

    cache_key = f"api_call_stats_{range_param}"
    data = cache.get(cache_key)

    if data is None:
        # Build chart data
        dates = []
        total_calls = []
        accuracy = []
        
        for d in _date_range(start, end):
            day_calls = CallLog.objects.filter(created_at__date=d)
            day_total = day_calls.count()
            day_ai = day_calls.exclude(intent_detected="speak_to_agent").filter(
                status="completed"
            ).count()
            
            dates.append(str(d))
            total_calls.append(day_total)
            accuracy.append(round(day_ai / day_total * 100, 1) if day_total else 0)
        
        chart_data = {
            "dates": dates,
            "total_calls": total_calls,
            "accuracy": accuracy,
        }

        # Top intents
        top_intents_qs = (
            CallLog.objects
            .filter(created_at__date__range=(start, end))
            .exclude(intent_detected="")
            .values("intent_detected")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:5]
        )
        top_intents = [
            {"intent": _intent_display(r["intent_detected"]), "count": r["cnt"]}
            for r in top_intents_qs
        ]

        # Hourly distribution (SQLite compatible)
        hourly = {}
        for hour in range(24):
            start_dt = datetime.combine(start, datetime.min.time().replace(hour=hour))
            end_dt = start_dt + timedelta(hours=1)
            count = CallLog.objects.filter(
                created_at__gte=start_dt,
                created_at__lt=end_dt
            ).count()
            hourly[hour] = count

        data = {
            "chart": chart_data,
            "top_intents": top_intents,
            "hourly": {
                "hours": list(range(24)),
                "counts": [hourly.get(h, 0) for h in range(24)],
            },
        }
        cache.set(cache_key, data, CACHE_TTL)

    return JsonResponse(data)


@login_required
@require_GET
def api_call_detail(request, call_id):
    """Full call details as JSON (for the dashboard detail modal)."""
    call = get_object_or_404(CallLog, pk=call_id)
    return JsonResponse({
        "id": call.pk,
        "caller_number": call.caller_number,
        "session_id": call.session_id,
        "status": call.status,
        "language_detected": call.language_detected,
        "intent_detected": _intent_display(call.intent_detected),
        "transcript": call.transcript,
        "response_given": call.response_given,
        "recording_url": call.recording_url,
        "duration_seconds": call.duration_seconds,
        "created_at": call.created_at.isoformat(),
    })


@login_required
@require_GET
def api_reminder_stats(request):
    """
    JSON reminder stats for the dashboard donut chart and cards.
    Returns zeros if the loans app is not installed.
    """
    try:
        from loans.models import PaymentReminder

        today = date.today()
        week_start, _ = _last_7_days()

        today_total = PaymentReminder.objects.filter(reminder_date=today).count()
        sent_today = PaymentReminder.objects.filter(
            reminder_date=today, status__in=["sent", "completed"]
        ).count()
        confirmed = PaymentReminder.objects.filter(
            reminder_date=today, payment_confirmed=True
        ).count()

        status_dist = (
            PaymentReminder.objects
            .filter(reminder_date__gte=week_start)
            .values("status")
            .annotate(cnt=Count("id"))
        )

        # Last 7 days: reminders sent per day
        daily_reminders = (
            PaymentReminder.objects
            .filter(reminder_date__range=(week_start, today))
            .values("reminder_date")
            .annotate(cnt=Count("id"))
        )
        daily_map = {str(r["reminder_date"]): r["cnt"] for r in daily_reminders}
        dates = [str(week_start + timedelta(days=i)) for i in range(7)]
        daily_counts = [daily_map.get(d, 0) for d in dates]

        return JsonResponse({
            "today_total": today_total,
            "sent_today": sent_today,
            "confirmed": confirmed,
            "confirmation_rate": round(confirmed / sent_today * 100, 1) if sent_today else 0,
            "status_distribution": {
                "labels": [r["status"].title() for r in status_dist],
                "counts": [r["cnt"] for r in status_dist],
            },
            "daily_trend": {
                "dates": dates,
                "counts": daily_counts,
            },
        })

    except Exception as exc:
        logger.debug("api_reminder_stats: loans app unavailable: %s", exc)
        return JsonResponse({
            "today_total": 0,
            "sent_today": 0,
            "confirmed": 0,
            "confirmation_rate": 0,
            "status_distribution": {"labels": [], "counts": []},
            "daily_trend": {"dates": [], "counts": []},
        })


# ── CSV Export ─────────────────────────────────────────────────────────────

@login_required
def export_report(request):
    """
    Download a CSV of CallLog rows for a date range.

    Query params
    ------------
    start : YYYY-MM-DD (default: 30 days ago)
    end   : YYYY-MM-DD (default: today)
    """
    import csv
    from django.http import HttpResponse
    
    default_start, _ = _last_30_days()
    default_end = date.today()

    # Parse dates from query params
    start_str = request.GET.get("start")
    end_str = request.GET.get("end")
    
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else default_start
    except:
        start = default_start
    
    try:
        end = datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else default_end
    except:
        end = default_end

    qs = CallLog.objects.filter(
        created_at__date__range=(start, end)
    ).order_by("-created_at")

    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="sacco_calls_{start}_{end}.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Date', 'Caller', 'Status', 'Language', 'Intent', 
        'Duration (s)', 'Transcript', 'Response'
    ])
    
    for call in qs:
        writer.writerow([
            call.created_at.strftime('%Y-%m-%d %H:%M'),
            call.caller_number,
            call.status,
            call.language_detected or '',
            call.intent_detected or '',
            call.duration_seconds,
            call.transcript[:200] + '...' if len(call.transcript) > 200 else call.transcript,
            call.response_given[:200] + '...' if len(call.response_given) > 200 else call.response_given,
        ])
    
    return response


# ── Loan Application Stats ──────────────────────────────────────────────────

def _get_application_summary(today: date) -> dict:
    """
    Return LoanApplication KPIs for dashboard cards.
    Returns zeros gracefully if the loans app is not installed.
    """
    try:
        from loans.models import LoanApplication
        from django.db.models import Sum

        qs_today = LoanApplication.objects.filter(
            application_date__date=today
        )
        apps_today = qs_today.count()

        # Status counts (all-time, for mini-cards)
        all_apps = LoanApplication.objects
        pending = all_apps.filter(status="pending_review").count()
        approved = all_apps.filter(status="approved").count()
        rejected = all_apps.filter(status="rejected").count()
        disbursed = all_apps.filter(status="disbursed").count()

        # Total approved amount this month
        month_start = today.replace(day=1)
        approved_amount = (
            LoanApplication.objects
            .filter(status__in=["approved", "disbursed"],
                    application_date__date__gte=month_start)
            .aggregate(total=Sum("amount_requested"))["total"] or 0
        )

        # Status distribution for bar chart (last 30 days)
        start_30, _ = _last_30_days()
        status_dist = (
            LoanApplication.objects
            .filter(application_date__date__gte=start_30)
            .values("status")
            .annotate(cnt=Count("id"))
            .order_by("status")
        )

        # Recent applications for dashboard table
        recent_apps = (
            LoanApplication.objects
            .select_related("member")
            .order_by("-application_date")[:6]
        )

        return {
            "apps_today": apps_today,
            "apps_pending": pending,
            "apps_approved": approved,
            "apps_rejected": rejected,
            "apps_disbursed": disbursed,
            "approved_amount_month": approved_amount,
            "app_status_dist_json": json.dumps({
                "labels": [r["status"].replace("_", " ").title() for r in status_dist],
                "counts": [r["cnt"] for r in status_dist],
            }),
            "recent_apps": recent_apps,
            "loans_applications_available": True,
        }
    except Exception as exc:
        logger.debug("application summary unavailable: %s", exc)
        return {
            "apps_today": 0,
            "apps_pending": 0,
            "apps_approved": 0,
            "apps_rejected": 0,
            "apps_disbursed": 0,
            "approved_amount_month": 0,
            "app_status_dist_json": json.dumps({"labels": [], "counts": []}),
            "recent_apps": [],
            "loans_applications_available": False,
        }


@login_required
@require_GET
def api_application_stats(request):
    """
    JSON endpoint for loan application stats.
    Used by the dashboard AJAX refresh and the applications_list mini-cards.
    """
    today = date.today()
    try:
        from loans.models import LoanApplication
        from django.db.models import Sum

        month_start = today.replace(day=1)
        start_30, _ = _last_30_days()

        status_dist = (
            LoanApplication.objects
            .filter(application_date__date__gte=start_30)
            .values("status")
            .annotate(cnt=Count("id"))
        )
        approved_amount = (
            LoanApplication.objects
            .filter(status__in=["approved", "disbursed"],
                    application_date__date__gte=month_start)
            .aggregate(total=Sum("amount_requested"))["total"] or 0
        )

        # Daily application counts for the last 7 days
        week_start, _ = _last_7_days()
        daily_counts = []
        for d in _date_range(week_start, today):
            count = LoanApplication.objects.filter(application_date__date=d).count()
            daily_counts.append(count)
        
        dates = [str(d) for d in _date_range(week_start, today)]

        return JsonResponse({
            "today": LoanApplication.objects.filter(application_date__date=today).count(),
            "pending": LoanApplication.objects.filter(status="pending_review").count(),
            "approved": LoanApplication.objects.filter(status="approved").count(),
            "rejected": LoanApplication.objects.filter(status="rejected").count(),
            "disbursed": LoanApplication.objects.filter(status="disbursed").count(),
            "approved_amount_month": float(approved_amount),
            "status_distribution": {
                "labels": [r["status"].replace("_", " ").title() for r in status_dist],
                "counts": [r["cnt"] for r in status_dist],
            },
            "daily_trend": {
                "dates": dates,
                "counts": daily_counts,
            },
        })
    except Exception as exc:
        logger.debug("api_application_stats unavailable: %s", exc)
        return JsonResponse({
            "today": 0,
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "disbursed": 0,
            "approved_amount_month": 0,
            "status_distribution": {"labels": [], "counts": []},
            "daily_trend": {"dates": [], "counts": []},
        })
