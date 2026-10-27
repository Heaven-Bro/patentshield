"""Django settings for PatentShield.

Local development values are read from the repository-root .env file. Production
must provide a non-placeholder DJANGO_SECRET_KEY and DATABASE_URL/DB credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BASE_DIR.parent

# Compose and native development both use the root .env; existing deployments
# may still place an environment file in backend/, so support it as a fallback.
load_dotenv(REPOSITORY_ROOT / ".env", override=False)
load_dotenv(BASE_DIR / ".env", override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> list[str]:
    values = os.getenv(name, default).split(",")
    return [part.strip() for part in values if part.strip()]


def _database_from_url(database_url: str) -> dict[str, object]:
    """Parse a PostgreSQL DATABASE_URL without requiring another package."""
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured(
            "DATABASE_URL must use the postgresql:// or postgres:// scheme."
        )
    if not parsed.path or parsed.path == "/":
        raise ImproperlyConfigured("DATABASE_URL must include a database name.")

    query = parse_qs(parsed.query)
    options: dict[str, str] = {}
    if query.get("sslmode"):
        options["sslmode"] = query["sslmode"][0]
    if query.get("application_name"):
        options["application_name"] = query["application_name"][0]

    config: dict[str, object] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
    if options:
        config["OPTIONS"] = options
    return config


DEBUG = _env_bool("DJANGO_DEBUG", False)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
_SECRET_PLACEHOLDERS = (
    "django-insecure-",
    "replace-this",
    "replace-with",
    "change-me",
    "dev-only",
    "your-secret",
)
if len(SECRET_KEY) < 50 or any(
    marker in SECRET_KEY.casefold() for marker in _SECRET_PLACEHOLDERS
):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a unique, non-placeholder value of at least "
        "50 characters. Generate it with Python's secrets.token_urlsafe(64)."
    )

ALLOWED_HOSTS = _env_csv("ALLOWED_HOSTS", "")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must contain at least one hostname.")
if not DEBUG and "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "accounts.apps.AccountsConfig",
    "pgvector",
    "corsheaders",
    "channels",
    "tenants",
    "audit",
    "patents",
    "indexing",
    "ingestion",
    "retrieval",
    "ai_engine",
    "analyses",
    "reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "audit.middleware.AuditLoggingMiddleware",
]

ROOT_URLCONF = "config.urls"
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
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    database_config = _database_from_url(DATABASE_URL)
    if not database_config.get("USER") or not database_config.get("PASSWORD"):
        raise ImproperlyConfigured(
            "DATABASE_URL must include an explicit database username and password."
        )
    database_password_from_url = str(database_config["PASSWORD"]).casefold()
    if len(database_password_from_url) < 16 or any(
        marker in database_password_from_url
        for marker in ("change-me", "replace-with", "password-here", "example")
    ):
        raise ImproperlyConfigured(
            "DATABASE_URL must use a non-placeholder database password of at least "
            "16 characters."
        )
    DATABASES = {"default": database_config}
else:
    database_name = os.getenv("DB_NAME", os.getenv("POSTGRES_DB", "")).strip()
    database_user = os.getenv("DB_USER", os.getenv("POSTGRES_USER", "")).strip()
    database_password = os.getenv(
        "DB_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")
    )
    if not database_name or not database_user or not database_password:
        raise ImproperlyConfigured(
            "Configure DATABASE_URL or explicitly provide DB_NAME, DB_USER, "
            "and DB_PASSWORD (or their POSTGRES_* equivalents)."
        )
    if len(database_password) < 16 or any(
        marker in database_password.casefold()
        for marker in ("change-me", "replace-with", "password-here", "example")
    ):
        raise ImproperlyConfigured(
            "The database password must be a non-placeholder value of at least "
            "16 characters."
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database_name,
            "USER": database_user,
            "PASSWORD": database_password,
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Dhaka")
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))

FRONTEND_ORIGINS = _env_csv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
CORS_ALLOWED_ORIGINS = FRONTEND_ORIGINS
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = _env_csv(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)

# Browser sessions are cookie-based; unsafe requests additionally require the
# CSRF token returned by /api/v1/auth/csrf/. Do not store session IDs in JS.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
SESSION_EXPIRE_AT_BROWSER_CLOSE = _env_bool(
    "SESSION_EXPIRE_AT_BROWSER_CLOSE", False
)

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CACHE_URL = os.getenv("REDIS_CACHE_URL", "redis://localhost:6379/1")
CHANNELS_REDIS_URL = os.getenv("CHANNELS_REDIS_URL", REDIS_URL)
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "1800"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "1500"))

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [CHANNELS_REDIS_URL]},
    }
}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "KEY_PREFIX": os.getenv("DJANGO_CACHE_KEY_PREFIX", "patentshield"),
        "TIMEOUT": 300,
    }
}

PATENTSHIELD_EXTERNAL_SEARCH_ENABLED = _env_bool(
    "PATENTSHIELD_EXTERNAL_SEARCH_ENABLED", False
)
PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE = _env_bool(
    "PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE", False
)
PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS = _env_bool(
    "PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS", False
)
PATENTSHIELD_EXTERNAL_SEARCH_ALLOWED_TENANTS = _env_csv(
    "PATENTSHIELD_EXTERNAL_SEARCH_ALLOWED_TENANTS", ""
)
PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY = os.getenv(
    "PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY", ""
).strip()
USPTO_ODP_API_KEY = os.getenv("USPTO_ODP_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
EPO_OPS_CLIENT_ID = os.getenv("EPO_OPS_CLIENT_ID", "").strip()
EPO_OPS_CLIENT_SECRET = os.getenv("EPO_OPS_CLIENT_SECRET", "").strip()
PATENTSHIELD_USPTO_API_URL = os.getenv(
    "PATENTSHIELD_USPTO_API_URL",
    "https://api.uspto.gov/api/v1/patent/applications/search",
).strip()
PATENTSHIELD_EPO_OPS_BASE_URL = os.getenv(
    "PATENTSHIELD_EPO_OPS_BASE_URL",
    "https://ops.epo.org/3.2/rest-services",
).strip()
PATENTSHIELD_EPO_OPS_TOKEN_URL = os.getenv(
    "PATENTSHIELD_EPO_OPS_TOKEN_URL",
    "https://ops.epo.org/3.2/auth/accesstoken",
).strip()
PATENTSHIELD_USPTO_MIN_INTERVAL_SECONDS = float(
    os.getenv("PATENTSHIELD_USPTO_MIN_INTERVAL_SECONDS", "1.0")
)
PATENTSHIELD_EPO_MIN_INTERVAL_SECONDS = float(
    os.getenv("PATENTSHIELD_EPO_MIN_INTERVAL_SECONDS", "6.0")
)
PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS = _env_bool(
    "PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS", DEBUG
)
PATENTSHIELD_MAX_PDF_UPLOAD_BYTES = int(
    os.getenv("PATENTSHIELD_MAX_PDF_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
if PATENTSHIELD_MAX_PDF_UPLOAD_BYTES <= 0:
    raise ImproperlyConfigured("PATENTSHIELD_MAX_PDF_UPLOAD_BYTES must be positive.")
if not DEBUG and PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS:
    raise ImproperlyConfigured(
        "Mock embeddings must be disabled when DJANGO_DEBUG=False."
    )
if PATENTSHIELD_EXTERNAL_SEARCH_ENABLED and not (
    USPTO_ODP_API_KEY
    or TAVILY_API_KEY
    or (EPO_OPS_CLIENT_ID and EPO_OPS_CLIENT_SECRET)
):
    raise ImproperlyConfigured(
        "External search is enabled, but no supported provider credentials are "
        "configured (USPTO_ODP_API_KEY, TAVILY_API_KEY, or EPO_OPS_CLIENT_ID/"
        "EPO_OPS_CLIENT_SECRET)."
    )
if (
    PATENTSHIELD_EXTERNAL_SEARCH_ENABLED
    or PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE
):
    if (
        len(PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY) < 32
        or any(
            marker in PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY.casefold()
            for marker in _SECRET_PLACEHOLDERS
        )
        or PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY == SECRET_KEY
    ):
        raise ImproperlyConfigured(
            "External search requires an independent random "
            "PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY of at least 32 characters."
        )

# Production transport and browser-origin protections. TLS termination must be
# configured at the trusted ingress before enabling SECURE_SSL_REDIRECT.
if not DEBUG:
    if not _env_bool("SESSION_COOKIE_SECURE", True):
        raise ImproperlyConfigured("SESSION_COOKIE_SECURE must be true in production.")
    if not _env_bool("CSRF_COOKIE_SECURE", True):
        raise ImproperlyConfigured("CSRF_COOKIE_SECURE must be true in production.")
    if not _env_bool("SECURE_SSL_REDIRECT", True):
        raise ImproperlyConfigured("SECURE_SSL_REDIRECT must be true in production.")
    if any(not origin.startswith("https://") for origin in CORS_ALLOWED_ORIGINS):
        raise ImproperlyConfigured(
            "Production CORS_ALLOWED_ORIGINS entries must use HTTPS."
        )
    if any(not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS):
        raise ImproperlyConfigured(
            "Production CSRF_TRUSTED_ORIGINS entries must use HTTPS."
        )
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", True)
    SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool(
        "SECURE_HSTS_INCLUDE_SUBDOMAINS", False
    )
    SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", False)
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
