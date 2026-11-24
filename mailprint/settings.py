from datetime import timedelta
from pathlib import Path

import dj_database_url
from decouple import Csv, config


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY", default="development-only-change-me")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config(
    "DJANGO_ALLOWED_HOSTS",
    default="127.0.0.1,localhost",
    cast=Csv(),
)
CSRF_TRUSTED_ORIGINS = config(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default="",
    cast=Csv(),
)
EDATS_API_BASE_URL = config(
    "EDATS_API_BASE_URL",
    default="https://tracking.penroaklan.com.ph/api/e_dats",
).rstrip("/")
EDATS_API_TOKEN = config("EDATS_API_TOKEN", default="")
EDATS_DOCUMENT_SEARCH_URL = config(
    "EDATS_DOCUMENT_SEARCH_URL",
    default="https://irs.penroaklan.com.ph/api/e_dats/document-paginated",
).rstrip("/")
EDATS_PUBLIC_TRACKING_URL = config(
    "EDATS_PUBLIC_TRACKING_URL",
    default="https://tracking.penroaklan.com.ph",
).rstrip("/")

INSTALLED_APPS = [
    "axes",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "mails",
    "printing",
    "rules",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "axes.middleware.AxesMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mailprint.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.mailprint_status",
            ],
        },
    },
]

WSGI_APPLICATION = "mailprint.wsgi.application"
ASGI_APPLICATION = "mailprint.asgi.application"

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = config("MAILPRINT_LOGIN_FAILURE_LIMIT", default=5, cast=int)
AXES_COOLOFF_TIME = timedelta(
    minutes=config("MAILPRINT_LOGIN_COOLOFF_MINUTES", default=15, cast=int)
)
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True

database_url = config("DATABASE_URL", default="")
if database_url:
    DATABASES = {"default": dj_database_url.parse(database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = config("DJANGO_TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = config(
    "DJANGO_SESSION_COOKIE_SECURE",
    default=not DEBUG,
    cast=bool,
)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = config(
    "DJANGO_CSRF_COOKIE_SECURE",
    default=not DEBUG,
    cast=bool,
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = config("DJANGO_SECURE_SSL_REDIRECT", default=False, cast=bool)

LOG_DIR = Path(config("MAILPRINT_LOG_DIR", default=str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "mailprint": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "mailprint_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "mailprint.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "mailprint",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["mailprint_file"],
            "level": "INFO",
            "propagate": False,
        },
        "mailprint": {
            "handlers": ["mailprint_file"],
            "level": "INFO",
            "propagate": False,
        },
        "waitress": {
            "handlers": ["mailprint_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

