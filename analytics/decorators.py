"""
analytics/decorators.py

Access-control decorators for the SACCO analytics dashboard.

Decorators
----------
sacco_required   — Ensures the user is authenticated AND is a staff member.
                   In single-SACCO mode (current), staff = SACCO manager.
                   In multi-SACCO mode, extend this to filter by user.sacco_id.

Usage
-----
    from analytics.decorators import sacco_required

    @sacco_required
    def dashboard_home(request):
        ...
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def sacco_required(view_func):
    """
    Composite decorator:
      1. Redirects unauthenticated users to the login page.
      2. Raises 403 for authenticated non-staff users.

    Multi-SACCO upgrade path
    ------------------------
    Replace the `is_staff` check with a SACCO membership check, e.g.:

        if not hasattr(request.user, 'sacco_profile'):
            raise PermissionDenied("No SACCO profile found.")

    Then use request.user.sacco_profile.sacco to filter all querysets.
    """

    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied(
                "Only SACCO managers (staff users) can access the analytics dashboard."
            )
        return view_func(request, *args, **kwargs)

    return _wrapped
