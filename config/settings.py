"""PlanVault settings. Everything environment-specific comes from env vars (see .env.example)."""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.environ.get("SECRET_KEY") or ("dev-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when DEBUG is off")

# Public base URL used in emails and Stripe redirects, e.g. https://plans.yourfirm.com
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/")

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
CSRF_TRUSTED_ORIGINS = [SITE_URL] if SITE_URL.startswith("https://") else []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "plans",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- storage
# Plan files are NEVER publicly served. Locally they live in ./private_files (not under any URL);
# in production they live in a private S3-compatible bucket (Cloudflare R2) and are handed out
# only through short-lived presigned URLs after the access checks in plans/views.py.
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

USE_S3 = env_bool("USE_S3", False)
if USE_S3:
    private_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["S3_BUCKET"],
            "endpoint_url": os.environ.get("S3_ENDPOINT_URL"),  # R2: https://<account>.r2.cloudflarestorage.com
            "access_key": os.environ["S3_ACCESS_KEY_ID"],
            "secret_key": os.environ["S3_SECRET_ACCESS_KEY"],
            "region_name": os.environ.get("S3_REGION", "auto"),
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": 300,  # presigned download links live 5 minutes
            "file_overwrite": False,
            "signature_version": "s3v4",
        },
    }
else:
    private_storage = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": BASE_DIR / "private_files", "base_url": None},
    }

STORAGES = {
    "default": private_storage,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if DEBUG:
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # bigger uploads stream to a temp file

# ---------------------------------------------------------------- email
# Resend and Postmark both offer SMTP, so plain Django SMTP is enough.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.resend.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "resend")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Plans <plans@example.com>")
# Where "client paid" / alert emails go. Falls back to the firm profile email.
ENGINEER_NOTIFY_EMAIL = os.environ.get("ENGINEER_NOTIFY_EMAIL", "")

# ---------------------------------------------------------------- stripe
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ---------------------------------------------------------------- plan delivery behaviour
PREVIEW_DPI = int(os.environ.get("PREVIEW_DPI", "100"))
PREVIEW_JPEG_QUALITY = int(os.environ.get("PREVIEW_JPEG_QUALITY", "70"))
# Optional box (fractions of page width/height: left,top,right,bottom) blanked out on every preview
# page, e.g. to hide the PE seal in the title block: "0.80,0.70,1.0,1.0". Empty = off.
PREVIEW_SEAL_BOX = os.environ.get("PREVIEW_SEAL_BOX", "")
# If a paid client can't be delivered their files within this many hours, they're refunded automatically.
DELIVERY_GUARANTEE_HOURS = int(os.environ.get("DELIVERY_GUARANTEE_HOURS", "24"))

# ---------------------------------------------------------------- security
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"  # the portal embeds its own preview PDF in an iframe

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ---------------------------------------------------------------- dashboard access (no login)
# Locally (DEBUG) the dashboard is simply at "/". Online it must be protected by a long random key and
# is then only reachable at /d/<DASHBOARD_KEY>/. Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(24))"
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "").strip()
if not DEBUG and len(DASHBOARD_KEY) < 20:
    raise RuntimeError("Set DASHBOARD_KEY (20+ random characters) before running online; the dashboard has no login.")
