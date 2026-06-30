"""
loans/urls.py

Include in config/urls.py:
    path("loans/", include("loans.urls")),
"""

from django.urls import path
from . import views

app_name = "loans"

urlpatterns = [
    # Payment reminders
    path("reminders/", views.reminders_list, name="reminders_list"),

    # Loan applications
    path("applications/",                     views.applications_list,   name="applications_list"),
    path("applications/<int:app_id>/",        views.application_detail,  name="application_detail"),
    path("applications/<int:app_id>/approve/", views.approve_application, name="approve_application"),
    path("applications/<int:app_id>/reject/",  views.reject_application,  name="reject_application"),
]
