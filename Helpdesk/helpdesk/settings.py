import os
from pathlib import Path

import dj_database_url
from decouple import Config, Csv, RepositoryEnv, config as auto_config

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR.parent / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

env = auto_config
if ENV_FILE.exists():
    env = Config(RepositoryEnv(str(ENV_FILE)))


def env_bool(name, default=False):
    value = env(name, default=None)
    if value is None:
        return default

    return str(value).strip().lower() in {"1", "true", "yes", "on", "debug"}


SECRET_KEY = env("SECRET_KEY", default="secret-key")
DEBUG = env_bool("DEBUG", default=False)
ALLOWED_HOSTS = env("ALLOWED_HOSTS", default="*", cast=Csv())
ORGANIZATION_NAME = env("ORGANIZATION_NAME", default="Helpdesk")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "widget_tweaks",
    "desk",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "helpdesk.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "libraries": {
                "custom_filters": "desk.templatetags.custom_filter",
            },
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "desk.context_processors.organization_context",
            ],
        },
    },
]

LOGIN_REDIRECT_URL = "/desk/"
LOGOUT_REDIRECT_URL = "/desk/login/"
LOGIN_URL = "/desk/login/"
LOGOUT_URL = "/desk/logout/"

WSGI_APPLICATION = "helpdesk.wsgi.application"

database_url = env("DATABASE_URL", default="sqlite:///db.sqlite3")
if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
    sqlite_name = database_url.removeprefix("sqlite:///")
    database_url = f"sqlite:///{(BASE_DIR / sqlite_name).as_posix()}"

DATABASES = {
    "default": dj_database_url.parse(database_url, conn_max_age=60),
}

AUTH_PASSWORD_VALIDATORS = []

AUTHENTICATION_BACKENDS = [
    "desk.forms.VerboseNameBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Yekaterinburg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_ROOT = os.path.join(BASE_DIR, "media")
MEDIA_URL = "/media/"
MAX_UPLOAD_SIZE_MB = env("MAX_UPLOAD_SIZE_MB", default=200, cast=int)
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = None

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_AGE = 1840

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    }
}

CACHE_MIDDLEWARE_ALIAS = "default"
CACHE_MIDDLEWARE_SECONDS = 0

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env("EMAIL_PORT", default=465, cast=int)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", default=EMAIL_PORT == 465)
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=EMAIL_PORT == 587)
EMAIL_TIMEOUT = env("EMAIL_TIMEOUT", default=10, cast=int)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = EMAIL_HOST_USER or "webmaster@localhost"
EMAIL_NOTIFICATIONS_ENABLED = env_bool("EMAIL_NOTIFICATIONS_ENABLED", default=bool(EMAIL_HOST))
EMAIL_SUBJECT_PREFIX = env("EMAIL_SUBJECT_PREFIX", default="[Helpdesk] ")

DEFAULT_ADMIN_USERNAME = env("DEFAULT_ADMIN_USERNAME", default="admin")
DEFAULT_ADMIN_PASSWORD = env("DEFAULT_ADMIN_PASSWORD", default="admin")
DEFAULT_ADMIN_EMAIL = env("DEFAULT_ADMIN_EMAIL", default="admin@example.com")
DEFAULT_ADMIN_VERBOSE_NAME = env("DEFAULT_ADMIN_VERBOSE_NAME", default="Администратор")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
        "simple": {
            "format": "[%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "helpdesk.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
        "desk": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
