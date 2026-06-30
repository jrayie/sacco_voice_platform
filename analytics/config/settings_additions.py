"""
config/settings_additions.py — ADDITIONS ONLY
==============================================
Merge these sections into your existing settings.py.
Do NOT paste this file wholesale — apply each section individually.
"""

# ── INSTALLED_APPS ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps
    "voice",
    "analytics",
    "loans",          # ← ADD THIS
]

# ── Africa's Talking ──────────────────────────────────────────────────────────
AT_USERNAME = "your_at_username"           # replace with real value
AT_API_KEY  = "your_at_api_key"            # replace with real value
AT_VOICE_NUMBER = "+254711XXXXXX"          # your AT virtual number

# URL Africa's Talking POSTs to when the member speaks during a reminder call
# Append ?reminder_id=<id> at call time (done in voice_service.py)
AT_VOICE_REMINDER_RESPONSE_URL = "https://your-domain.com/api/voice/reminder-response/"
AT_VOICE_REMINDER_CALLBACK_URL = "https://your-domain.com/api/voice/reminder-callback/"

# ── SACCO manager email (receives daily summary from check_due_payments) ──────
SACCO_MANAGER_EMAIL = "manager@yoursacco.co.ke"

# ── Email backend ─────────────────────────────────────────────────────────────
# Development — print to console:
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Production — use SES (pip install django-ses):
# EMAIL_BACKEND   = "django_ses.SESBackend"
# AWS_SES_REGION_NAME     = "us-east-1"
# AWS_SES_REGION_ENDPOINT = "email.us-east-1.amazonaws.com"
# DEFAULT_FROM_EMAIL      = "noreply@yoursacco.co.ke"

# ── CACHES ────────────────────────────────────────────────────────────────────
# Development:
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "sacco-platform",
    }
}
# Production (pip install django-redis):
# CACHES = {
#     "default": {
#         "BACKEND": "django.core.cache.backends.redis.RedisCache",
#         "LOCATION": "redis://127.0.0.1:6379/1",
#     }
# }

# ── Auth ──────────────────────────────────────────────────────────────────────
LOGIN_URL          = "/admin/login/"
LOGIN_REDIRECT_URL = "/analytics/"

# ── Timezone — important for DND hour checks ──────────────────────────────────
TIME_ZONE  = "Africa/Nairobi"
USE_TZ     = True

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {module}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file_loans": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "/var/log/sacco/loans.log",
            "maxBytes": 10 * 1024 * 1024,   # 10 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "analytics": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "voice":     {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "loans":     {"handlers": ["console", "file_loans"], "level": "DEBUG", "propagate": False},
    },
}
