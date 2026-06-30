
from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "SACCO Voice Platform"
admin.site.site_title  = "SACCO Admin"
admin.site.index_title = "Administration"

urlpatterns = [
    path("admin/", admin.site.urls),

    # Voice webhooks (Africa's Talking inbound + outbound reminder callbacks)
    path("api/voice/", include("voice.urls")),

    # Analytics dashboard for SACCO managers
    path("analytics/", include("analytics.urls")),

    # Loans & payment reminders
    path("loans/", include("loans.urls")),
]
