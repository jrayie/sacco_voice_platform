"""
analytics/urls.py

Include in config/urls.py:
    path("analytics/", include("analytics.urls")),
"""

from django.urls import path
from . import views

app_name = "analytics"

urlpatterns = [
    # Dashboard pages
    path("",                    views.dashboard_home, name="home"),
    path("call/<int:call_id>/", views.call_detail,    name="call_detail"),

    # JSON APIs (AJAX chart refresh)
    path("api/stats/",               views.api_call_stats,       name="api_stats"),
    path("api/call/<int:call_id>/",  views.api_call_detail,      name="api_call_detail"),
    path("api/reminder-stats/",      views.api_reminder_stats,   name="api_reminder_stats"),
    path("api/application-stats/",   views.api_application_stats, name="api_application_stats"),

    # CSV export
    path("export/csv/", views.export_report, name="export_csv"),
]
