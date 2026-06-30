
"""
analytics/export.py

Reusable CSV export helper for CallLog querysets.
Called by analytics/views.py → export_report, but can also be imported
by management commands or API views.

Usage
-----
    from analytics.export import calllog_queryset_to_csv
    from voice.models import CallLog

    qs = CallLog.objects.filter(created_at__date__range=(start, end))
    response = calllog_queryset_to_csv(qs, filename="my_export.csv")
    return response
"""

import csv
from django.http import HttpResponse


# Columns in the CSV output, in order.
CSV_COLUMNS = [
    ("Date",          lambda c: c.created_at.strftime("%Y-%m-%d")),
    ("Time (UTC)",    lambda c: c.created_at.strftime("%H:%M:%S")),
    ("Caller Number", lambda c: c.caller_number),
    ("Session ID",    lambda c: c.session_id),
    ("Status",        lambda c: c.status),
    ("Language",      lambda c: c.language_detected),
    ("Intent",        lambda c: c.intent_detected),
    ("Duration (s)",  lambda c: c.duration_seconds),
    ("Transcript",    lambda c: c.transcript),
    ("AI Response",   lambda c: c.response_given),
    ("Recording URL", lambda c: c.recording_url),
]


def calllog_queryset_to_csv(queryset, filename: str = "sacco_calls.csv") -> HttpResponse:
    """
    Stream a CallLog queryset as a downloadable CSV HttpResponse.

    Args:
        queryset : A (possibly filtered/ordered) CallLog queryset.
        filename : Suggested download filename shown to the browser.

    Returns:
        HttpResponse with Content-Type 'text/csv' and Content-Disposition header.

    Notes
    -----
    Uses a streaming writer so even large querysets don't exhaust memory.
    The queryset is evaluated lazily row-by-row via Django's iterator().
    """
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # UTF-8 BOM so Excel opens the file without encoding issues
    response.write("\ufeff")

    writer = csv.writer(response)

    # Header row
    writer.writerow([col_name for col_name, _ in CSV_COLUMNS])

    # Data rows — iterator() avoids loading the full queryset into memory
    for call in queryset.iterator(chunk_size=500):
        writer.writerow([extractor(call) for _, extractor in CSV_COLUMNS])

    return response
