"""Django settings for PatentShield.

Local development values are read from the repository-root .env file. Production
must provide a non-placeholder DJANGO_SECRET_KEY and DATABASE_URL/DB credentials.
"""

from __future__ import annotations
from corsheaders.defaults import default_headers
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from config.environment import load_project_environment
from django.core.exceptions import ImproperlyConfigured
import sys

TESTING = 'test' in sys.argv or 'pytest' in sys.modules

if TESTING:
    SECURE_SSL_REDIRECT = False
BASE_DIR = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BASE_DIR.parent

# There is one local-development source of dotenv configuration: the
# repository-root .env. backend/.env is deliberately not loaded because legacy
# files there may contain stale/short secrets that fail or weaken startup policy.
# Production containers should inject secrets through the orchestrator.
load_project_environment(REPOSITORY_ROOT)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> list[str]:
    values = os.getenv(name, default).split(",")
    return [part.strip().strip("'\"").rstrip("/") for part in values if part.strip()]


# Phase 9 air-gapped mode disables Hugging Face Hub/Transformers network access
# before any model library is imported. Kubernetes NetworkPolicy remains the
# actual egress boundary; these flags are defense in depth.
PATENTSHIELD_OFFLINE_MODE = _env_bool("PATENTSHIELD_OFFLINE_MODE", False)
if PATENTSHIELD_OFFLINE_MODE:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    # These legacy task gates read directly from os.environ, so override them
    # too rather than relying only on settings constants later in this module.
    os.environ["PATENTSHIELD_EXTERNAL_SEARCH_ENABLED"] = "false"
    os.environ["PATENTSHIELD_TRANSLATION_ENABLED"] = "false"
    os.environ["PATENTSHIELD_SURVEILLANCE_ENABLED"] = "false"
    os.environ["PATENTSHIELD_CONNECTOR_SYNC_ENABLED"] = "false"


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
    "office_actions.apps.OfficeActionsConfig",
    "multilingual_search.apps.MultilingualSearchConfig",
    "fto.apps.FTOConfig",
    "landscapes.apps.LandscapesConfig",
    "portfolio_valuation.apps.PortfolioValuationConfig",
    "enterprise_identity.apps.EnterpriseIdentityConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "enterprise_identity.middleware.ManagedSessionTrackingMiddleware",
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

# Model paths are ordinary local files in air-gapped deployments. Keep canonical
# model IDs separate from local paths. Model identity is persisted
# with every vector row, while deployment paths may differ between environments.
BGE_MODEL_NAME = os.getenv("BGE_MODEL_NAME", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
BGE_MODEL_PATH = os.getenv("PATENTSHIELD_BGE_MODEL_PATH", "").strip()
BGE_MODEL_REVISION = os.getenv("PATENTSHIELD_BGE_MODEL_REVISION", "main").strip() or "main"
BGE_BATCH_SIZE = int(os.getenv("PATENTSHIELD_BGE_BATCH_SIZE", "32"))
PATENTSHIELD_BGE_MAX_LENGTH = int(os.getenv("PATENTSHIELD_BGE_MAX_LENGTH", "8192"))
PATENTSHIELD_BGE_USE_FP16 = _env_bool("PATENTSHIELD_BGE_USE_FP16", False)
PATENTSHIELD_BGE_DEVICE = os.getenv("PATENTSHIELD_BGE_DEVICE", "").strip()
PATENTSHIELD_MODEL_CACHE_DIR = os.getenv("PATENTSHIELD_MODEL_CACHE_DIR", "").strip()
if not 1 <= BGE_BATCH_SIZE <= 256:
    raise ImproperlyConfigured("PATENTSHIELD_BGE_BATCH_SIZE must be between 1 and 256.")
if not 128 <= PATENTSHIELD_BGE_MAX_LENGTH <= 8192:
    raise ImproperlyConfigured("PATENTSHIELD_BGE_MAX_LENGTH must be between 128 and 8192.")
PATENTSHIELD_RERANKER_MODEL_NAME = (
    os.getenv("PATENTSHIELD_RERANKER_MODEL_NAME", "BAAI/bge-reranker-v2-m3").strip()
    or "BAAI/bge-reranker-v2-m3"
)
PATENTSHIELD_RERANKER_MODEL_PATH = os.getenv("PATENTSHIELD_RERANKER_MODEL_PATH", "").strip()
PATENTSHIELD_RERANKER_MODEL_REVISION = (
    os.getenv("PATENTSHIELD_RERANKER_MODEL_REVISION", "main").strip() or "main"
)
PATENTSHIELD_RERANKER_USE_FP16 = _env_bool("PATENTSHIELD_RERANKER_USE_FP16", False)
PATENTSHIELD_RERANKER_DEVICE = os.getenv("PATENTSHIELD_RERANKER_DEVICE", "").strip()
PATENTSHIELD_RERANKER_QUERY_MAX_LENGTH = int(
    os.getenv("PATENTSHIELD_RERANKER_QUERY_MAX_LENGTH", "256")
)
PATENTSHIELD_RERANKER_MAX_LENGTH = int(
    os.getenv("PATENTSHIELD_RERANKER_MAX_LENGTH", "512")
)
if not 64 <= PATENTSHIELD_RERANKER_QUERY_MAX_LENGTH <= 1024:
    raise ImproperlyConfigured("PATENTSHIELD_RERANKER_QUERY_MAX_LENGTH must be between 64 and 1024.")
if not 128 <= PATENTSHIELD_RERANKER_MAX_LENGTH <= 2048:
    raise ImproperlyConfigured("PATENTSHIELD_RERANKER_MAX_LENGTH must be between 128 and 2048.")

# Public patent semantic search uses BAAI/bge-large-en-v1.5 (1,024 dimensions).
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME = (
    os.getenv("PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME", "").strip()
    or os.getenv("PATENTSHIELD_MINILM_MODEL_PATH", "").strip()
    or os.getenv("PATENTSHIELD_MINILM_MODEL_NAME", "BAAI/bge-large-en-v1.5").strip()
    or "BAAI/bge-large-en-v1.5"
)
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION = (
    os.getenv("PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION", "").strip()
    or os.getenv("PATENTSHIELD_PUBLIC_PATENT_MODEL_REVISION", "main").strip()
    or "main"
)
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE = int(
    os.getenv("PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE", os.getenv("PATENTSHIELD_MINILM_BATCH_SIZE", "32"))
)
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_DEVICE = os.getenv(
    "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_DEVICE", os.getenv("PATENTSHIELD_MINILM_DEVICE", "")
).strip()
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_CACHE_DIR = os.getenv(
    "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_CACHE_DIR", ""
).strip()
PATENTSHIELD_MODEL_BUNDLE_MANIFEST = os.getenv(
    "PATENTSHIELD_MODEL_BUNDLE_MANIFEST", ""
).strip()
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_LOCAL_FILES_ONLY = _env_bool(
    "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_LOCAL_FILES_ONLY",
    _env_bool("PATENTSHIELD_MINILM_LOCAL_FILES_ONLY", PATENTSHIELD_OFFLINE_MODE),
)
PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_PRELOAD_ON_STARTUP = _env_bool(
    "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_PRELOAD_ON_STARTUP",
    _env_bool("PATENTSHIELD_MINILM_PRELOAD_ON_STARTUP", False),
)
# Backward-compatible aliases for existing deployment manifests and tests.
PATENTSHIELD_MINILM_MODEL_NAME = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME
PATENTSHIELD_MINILM_REVISION = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION
PATENTSHIELD_MINILM_BATCH_SIZE = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE
PATENTSHIELD_MINILM_DEVICE = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_DEVICE
PATENTSHIELD_MINILM_LOCAL_FILES_ONLY = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_LOCAL_FILES_ONLY
PATENTSHIELD_MINILM_PRELOAD_ON_STARTUP = PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_PRELOAD_ON_STARTUP
if not 1 <= PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE <= 64:
    raise ImproperlyConfigured(
        "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE must be between 1 and 64."
    )
PATENTSHIELD_TEXT_EMBEDDING_BATCH_SIZE = int(os.getenv("PATENTSHIELD_TEXT_EMBEDDING_BATCH_SIZE", "32"))
if not 1 <= PATENTSHIELD_TEXT_EMBEDDING_BATCH_SIZE <= 64:
    raise ImproperlyConfigured("PATENTSHIELD_TEXT_EMBEDDING_BATCH_SIZE must be between 1 and 64.")
PATENTSHIELD_RERANKER_BATCH_SIZE = int(os.getenv("PATENTSHIELD_RERANKER_BATCH_SIZE", "16"))
if not 1 <= PATENTSHIELD_RERANKER_BATCH_SIZE <= 64:
    raise ImproperlyConfigured("PATENTSHIELD_RERANKER_BATCH_SIZE must be between 1 and 64.")
PATENTSHIELD_BM25_MAX_CHUNKS = int(os.getenv("PATENTSHIELD_BM25_MAX_CHUNKS", "2500"))
if not 1 <= PATENTSHIELD_BM25_MAX_CHUNKS <= 5000:
    raise ImproperlyConfigured("PATENTSHIELD_BM25_MAX_CHUNKS must be between 1 and 5000.")
PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS = int(
    os.getenv("PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS", "60000")
)
if not 5_000 <= PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS <= 100_000:
    raise ImproperlyConfigured("PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS must be between 5000 and 100000.")
PATENTSHIELD_LIMITATION_RETRIEVAL_TOP_N = int(os.getenv("PATENTSHIELD_LIMITATION_RETRIEVAL_TOP_N", "5"))
if not 1 <= PATENTSHIELD_LIMITATION_RETRIEVAL_TOP_N <= 50:
    raise ImproperlyConfigured("PATENTSHIELD_LIMITATION_RETRIEVAL_TOP_N must be between 1 and 50.")

# Private object storage is optional. The filesystem backend remains the default
# for existing deployments; production object-storage mode requires explicit
# server-side encryption using a customer-managed KMS key.
PATENTSHIELD_STORAGE_BACKEND = os.getenv(
    "PATENTSHIELD_STORAGE_BACKEND", "filesystem"
).strip().casefold()
if PATENTSHIELD_STORAGE_BACKEND not in {"filesystem", "s3"}:
    raise ImproperlyConfigured(
        "PATENTSHIELD_STORAGE_BACKEND must be either 'filesystem' or 's3'."
    )
if PATENTSHIELD_STORAGE_BACKEND == "s3":
    _storage_bucket = os.getenv("AWS_STORAGE_BUCKET_NAME", "").strip()
    _storage_endpoint = os.getenv("AWS_S3_ENDPOINT_URL", "").strip().rstrip("/")
    _storage_region = os.getenv("AWS_S3_REGION_NAME", "").strip()
    _storage_sse = os.getenv("PATENTSHIELD_OBJECT_STORAGE_SSE", "aws:kms").strip()
    _storage_kms_key_id = os.getenv(
        "PATENTSHIELD_OBJECT_STORAGE_KMS_KEY_ID", ""
    ).strip()
    _storage_verify = os.getenv("AWS_S3_VERIFY", "true").strip()
    _storage_url = urlsplit(_storage_endpoint)
    if not _storage_bucket or not _storage_endpoint or not _storage_region:
        raise ImproperlyConfigured(
            "S3 storage requires AWS_STORAGE_BUCKET_NAME, AWS_S3_ENDPOINT_URL, "
            "and AWS_S3_REGION_NAME. Point the endpoint at private object storage."
        )
    if (
        _storage_url.scheme not in {"https", "http"}
        or not _storage_url.hostname
        or _storage_url.username
        or _storage_url.password
        or _storage_url.query
        or _storage_url.fragment
    ):
        raise ImproperlyConfigured(
            "AWS_S3_ENDPOINT_URL must be a base HTTP(S) URL without credentials, "
            "query, or fragment."
        )
    if not DEBUG and _storage_url.scheme != "https":
        raise ImproperlyConfigured(
            "Private object storage must use HTTPS in non-debug deployments."
        )
    if _storage_sse != "aws:kms" or not _storage_kms_key_id:
        raise ImproperlyConfigured(
            "S3 object storage requires PATENTSHIELD_OBJECT_STORAGE_SSE=aws:kms "
            "and a customer-managed PATENTSHIELD_OBJECT_STORAGE_KMS_KEY_ID. "
            "Use an encrypted filesystem/PVC instead if your isolated object-store "
            "implementation does not expose this S3 encryption contract."
        )
    if _storage_verify.casefold() in {"0", "false", "no", "off"} and not DEBUG:
        raise ImproperlyConfigured(
            "AWS_S3_VERIFY cannot disable TLS certificate verification in production."
        )
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": _storage_bucket,
                "endpoint_url": _storage_endpoint,
                "region_name": _storage_region,
                "addressing_style": os.getenv("AWS_S3_ADDRESSING_STYLE", "path").strip(),
                "verify": (
                    False if _storage_verify.casefold() in {"0", "false", "no", "off"}
                    else (_storage_verify if _storage_verify.casefold() not in {"1", "true", "yes", "on"} else True)
                ),
                "default_acl": None,
                "querystring_auth": True,
                "file_overwrite": False,
                "object_parameters": {
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": _storage_kms_key_id,
                },
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }

FRONTEND_ORIGINS = _env_csv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
CORS_ALLOWED_ORIGINS = FRONTEND_ORIGINS
CORS_ALLOW_ALL_ORIGINS = _env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)  # <--- এই লাইনটি যোগ করুন
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = _env_csv(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)
CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-tenant-id",
]
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
    "DEFAULT_THROTTLE_RATES": {
        "login": os.getenv("PATENTSHIELD_LOGIN_RATE", "10/min"),
    },
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
) and not PATENTSHIELD_OFFLINE_MODE
# Phase 3 surveillance is opt-in and reuses the existing external-search policy.
PATENTSHIELD_SURVEILLANCE_ENABLED = _env_bool(
    "PATENTSHIELD_SURVEILLANCE_ENABLED", False
) and not PATENTSHIELD_OFFLINE_MODE
PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY = os.getenv(
    "PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY", ""
).strip()
PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS = _env_csv(
    "PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS", ""
)
PATENTSHIELD_SURVEILLANCE_MAX_RESULTS = int(
    os.getenv("PATENTSHIELD_SURVEILLANCE_MAX_RESULTS", "10")
)
PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS = int(
    os.getenv("PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS", "6")
)
if not 1 <= PATENTSHIELD_SURVEILLANCE_MAX_RESULTS <= 25:
    raise ImproperlyConfigured(
        "PATENTSHIELD_SURVEILLANCE_MAX_RESULTS must be between 1 and 25."
    )
if not 1 <= PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS <= 12:
    raise ImproperlyConfigured(
        "PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS must be between 1 and 12."
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

# Phase 5 — OpenAI-compatible Meta LLM endpoint (Muse Spark 1.3).
# A missing key/base URL disables these external inference features by default.
META_LLM_API_KEY = os.getenv("META_LLM_API_KEY", "").strip()
META_LLM_BASE_URL = os.getenv("META_LLM_BASE_URL", "").strip().rstrip("/")
META_LLM_MODEL = os.getenv("META_LLM_MODEL", "Muse Spark 1.3").strip() or "Muse Spark 1.3"
META_LLM_TIMEOUT_SECONDS = float(os.getenv("META_LLM_TIMEOUT_SECONDS", "60"))
META_LLM_ENABLED = _env_bool(
    "META_LLM_ENABLED", bool(META_LLM_API_KEY and META_LLM_BASE_URL)
) and not PATENTSHIELD_OFFLINE_MODE
META_LLM_ALLOW_SENSITIVE_DATA = _env_bool(
    "META_LLM_ALLOW_SENSITIVE_DATA", False
)
if META_LLM_TIMEOUT_SECONDS <= 0:
    raise ImproperlyConfigured("META_LLM_TIMEOUT_SECONDS must be positive.")
if META_LLM_BASE_URL:
    _meta_llm_url = urlsplit(META_LLM_BASE_URL)
    if (
        _meta_llm_url.scheme not in {"http", "https"}
        or not _meta_llm_url.netloc
        or _meta_llm_url.username
        or _meta_llm_url.password
        or _meta_llm_url.query
        or _meta_llm_url.fragment
    ):
        raise ImproperlyConfigured(
            "META_LLM_BASE_URL must be an absolute HTTP(S) base URL without credentials, query, or fragment."
        )
    if not DEBUG and _meta_llm_url.scheme != "https":
        raise ImproperlyConfigured("META_LLM_BASE_URL must use HTTPS in production.")
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

# Phase 5 — on-demand translation is an explicit disclosure operation. It stays
# disabled until a provider endpoint and per-tenant allowlist are configured.
PATENTSHIELD_TRANSLATION_ENABLED = _env_bool(
    "PATENTSHIELD_TRANSLATION_ENABLED", False
) and not PATENTSHIELD_OFFLINE_MODE
PATENTSHIELD_TRANSLATION_URL = os.getenv(
    "PATENTSHIELD_TRANSLATION_URL", ""
).strip().rstrip("/")
PATENTSHIELD_TRANSLATION_API_KEY = os.getenv(
    "PATENTSHIELD_TRANSLATION_API_KEY", ""
).strip()
PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS = _env_csv(
    "PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS", ""
)
PATENTSHIELD_TRANSLATION_ENGINE_VERSION = os.getenv(
    "PATENTSHIELD_TRANSLATION_ENGINE_VERSION", "configured-v1"
).strip() or "configured-v1"
if len(PATENTSHIELD_TRANSLATION_ENGINE_VERSION) > 80:
    raise ImproperlyConfigured(
        "PATENTSHIELD_TRANSLATION_ENGINE_VERSION must be at most 80 characters."
    )
PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS = float(
    os.getenv("PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS", "15")
)
PATENTSHIELD_TRANSLATION_MAX_INPUT_CHARS = int(
    os.getenv("PATENTSHIELD_TRANSLATION_MAX_INPUT_CHARS", "8000")
)
PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS = int(
    os.getenv("PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS", "30000")
)
PATENTSHIELD_TRANSLATION_MAX_RESPONSE_BYTES = int(
    os.getenv("PATENTSHIELD_TRANSLATION_MAX_RESPONSE_BYTES", str(1024 * 1024))
)
if not 1_024 <= PATENTSHIELD_TRANSLATION_MAX_RESPONSE_BYTES <= 10 * 1024 * 1024:
    raise ImproperlyConfigured("PATENTSHIELD_TRANSLATION_MAX_RESPONSE_BYTES must be between 1024 and 10485760.")
if not 1 <= PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS <= 60:
    raise ImproperlyConfigured(
        "PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS must be between 1 and 60."
    )
if not 100 <= PATENTSHIELD_TRANSLATION_MAX_INPUT_CHARS <= 20000:
    raise ImproperlyConfigured(
        "PATENTSHIELD_TRANSLATION_MAX_INPUT_CHARS must be between 100 and 20000."
    )
if not 100 <= PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS <= 60000:
    raise ImproperlyConfigured(
        "PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS must be between 100 and 60000."
    )
if PATENTSHIELD_TRANSLATION_ENABLED:
    translation_url_parts = urlsplit(PATENTSHIELD_TRANSLATION_URL)
    if (
        translation_url_parts.scheme not in {"https", "http"}
        or not translation_url_parts.hostname
        or translation_url_parts.username
        or translation_url_parts.password
        or translation_url_parts.query
        or translation_url_parts.fragment
    ):
        raise ImproperlyConfigured(
            "PATENTSHIELD_TRANSLATION_URL must be a base HTTP(S) URL without "
            "embedded credentials, query parameters, or a fragment."
        )
    if not DEBUG and translation_url_parts.scheme != "https":
        raise ImproperlyConfigured(
            "Production translation provider URLs must use HTTPS."
        )
    if not PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS:
        raise ImproperlyConfigured(
            "Translation is enabled but PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS "
            "is empty; explicitly authorize the tenants permitted to disclose "
            "selected patent text to the translation service."
        )

# Phase 8 — enterprise identity integrations. Federation stays fail-closed until
# a tenant owner configures and explicitly enables an identity provider.
PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY = os.getenv(
    "PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY", ""
).strip()
PATENTSHIELD_OIDC_CALLBACK_URL = os.getenv(
    "PATENTSHIELD_OIDC_CALLBACK_URL", ""
).strip()
PATENTSHIELD_SAML_ACS_BASE_URL = os.getenv(
    "PATENTSHIELD_SAML_ACS_BASE_URL", ""
).strip().rstrip("/")
PATENTSHIELD_SAML_METADATA_BASE_URL = os.getenv(
    "PATENTSHIELD_SAML_METADATA_BASE_URL", ""
).strip().rstrip("/")
PATENTSHIELD_SSO_SUCCESS_URL = os.getenv(
    "PATENTSHIELD_SSO_SUCCESS_URL", "/"
).strip() or "/"
PATENTSHIELD_IDP_ALLOWED_HOSTS = os.getenv("PATENTSHIELD_IDP_ALLOWED_HOSTS", "").strip()
PATENTSHIELD_REPORT_FONT_PATH = os.getenv("PATENTSHIELD_REPORT_FONT_PATH", "").strip() or None
PATENTSHIELD_SCIM_MAX_PAGE_SIZE = int(
    os.getenv("PATENTSHIELD_SCIM_MAX_PAGE_SIZE", "100")
)
if not 1 <= PATENTSHIELD_SCIM_MAX_PAGE_SIZE <= 100:
    raise ImproperlyConfigured(
        "PATENTSHIELD_SCIM_MAX_PAGE_SIZE must be between 1 and 100."
    )

PATENTSHIELD_MAX_PDF_UPLOAD_BYTES = int(
    os.getenv("PATENTSHIELD_MAX_PDF_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
if PATENTSHIELD_MAX_PDF_UPLOAD_BYTES <= 0:
    raise ImproperlyConfigured("PATENTSHIELD_MAX_PDF_UPLOAD_BYTES must be positive.")
PATENTSHIELD_PDF_MAX_BYTES = int(os.getenv("PATENTSHIELD_PDF_MAX_BYTES", str(PATENTSHIELD_MAX_PDF_UPLOAD_BYTES)))
PATENTSHIELD_PDF_MAX_PAGES = int(os.getenv("PATENTSHIELD_PDF_MAX_PAGES", "2000"))
PATENTSHIELD_PDF_MAX_TEXT_CHARS = int(os.getenv("PATENTSHIELD_PDF_MAX_TEXT_CHARS", "25000000"))
PATENTSHIELD_PDF_OCR_MIN_PAGE_CHARS = int(os.getenv("PATENTSHIELD_PDF_OCR_MIN_PAGE_CHARS", "40"))
PATENTSHIELD_PDF_MAX_OCR_PAGES = int(os.getenv("PATENTSHIELD_PDF_MAX_OCR_PAGES", "250"))
PATENTSHIELD_PDF_MAX_RENDER_PIXELS = int(os.getenv("PATENTSHIELD_PDF_MAX_RENDER_PIXELS", "24000000"))
PATENTSHIELD_PDF_OCR_PAGE_TIMEOUT_SECONDS = int(os.getenv("PATENTSHIELD_PDF_OCR_PAGE_TIMEOUT_SECONDS", "30"))
PATENTSHIELD_PDF_TOTAL_TIMEOUT_SECONDS = int(os.getenv("PATENTSHIELD_PDF_TOTAL_TIMEOUT_SECONDS", "900"))
PATENTSHIELD_PDF_OCR_LANGUAGE = os.getenv("PATENTSHIELD_PDF_OCR_LANGUAGE", "eng").strip() or "eng"
PATENTSHIELD_TESSERACT_BINARY = os.getenv("PATENTSHIELD_TESSERACT_BINARY", "tesseract").strip() or "tesseract"
PATENTSHIELD_PDF_MAX_SCHEMATICS = int(os.getenv("PATENTSHIELD_PDF_MAX_SCHEMATICS", "200"))
PATENTSHIELD_PDF_SCHEMATIC_MAX_PIXELS = int(os.getenv("PATENTSHIELD_PDF_SCHEMATIC_MAX_PIXELS", "8000000"))
PATENTSHIELD_PDF_SCHEMATIC_MAX_BYTES = int(os.getenv("PATENTSHIELD_PDF_SCHEMATIC_MAX_BYTES", str(20 * 1024 * 1024)))
PATENTSHIELD_PDF_SCHEMATIC_TIMEOUT_SECONDS = int(os.getenv("PATENTSHIELD_PDF_SCHEMATIC_TIMEOUT_SECONDS", "600"))
if any(value <= 0 for value in (
    PATENTSHIELD_PDF_MAX_BYTES, PATENTSHIELD_PDF_MAX_PAGES,
    PATENTSHIELD_PDF_MAX_TEXT_CHARS, PATENTSHIELD_PDF_OCR_MIN_PAGE_CHARS,
    PATENTSHIELD_PDF_MAX_OCR_PAGES, PATENTSHIELD_PDF_MAX_RENDER_PIXELS,
    PATENTSHIELD_PDF_OCR_PAGE_TIMEOUT_SECONDS, PATENTSHIELD_PDF_TOTAL_TIMEOUT_SECONDS,
    PATENTSHIELD_PDF_MAX_SCHEMATICS, PATENTSHIELD_PDF_SCHEMATIC_MAX_PIXELS,
    PATENTSHIELD_PDF_SCHEMATIC_MAX_BYTES, PATENTSHIELD_PDF_SCHEMATIC_TIMEOUT_SECONDS,
)):
    raise ImproperlyConfigured("All PATENTSHIELD_PDF_* resource limits must be positive.")
if PATENTSHIELD_PDF_MAX_BYTES > PATENTSHIELD_MAX_PDF_UPLOAD_BYTES:
    raise ImproperlyConfigured("PATENTSHIELD_PDF_MAX_BYTES cannot exceed the upload byte limit.")
PATENTSHIELD_FTO_MAX_UPLOAD_BYTES = int(
    os.getenv("PATENTSHIELD_FTO_MAX_UPLOAD_BYTES", str(min(PATENTSHIELD_MAX_PDF_UPLOAD_BYTES, 30 * 1024 * 1024)))
)
if PATENTSHIELD_FTO_MAX_UPLOAD_BYTES <= 0 or PATENTSHIELD_FTO_MAX_UPLOAD_BYTES > PATENTSHIELD_MAX_PDF_UPLOAD_BYTES:
    raise ImproperlyConfigured(
        "PATENTSHIELD_FTO_MAX_UPLOAD_BYTES must be positive and no greater than PATENTSHIELD_MAX_PDF_UPLOAD_BYTES."
    )
# Startup preflight: mock embeddings are never a valid production runtime.
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
    SECURE_SSL_REDIRECT = False if TESTING else _env_bool("SECURE_SSL_REDIRECT", True)
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