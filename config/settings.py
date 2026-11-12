"""Настройки spread_terminal. Время везде UTC, как в strategies.py."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env(BASE_DIR / ".env")

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
# На сервере: DJANGO_ALLOWED_HOSTS=172.16.207.66,spread.example.ru — адреса, по которым открывают сайт.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"] + [
    host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()
]
# Вход в админку по https требует адрес сайта: https://spread.example.ru
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()
]
if not DEBUG and not SECRET_KEY:
    raise RuntimeError("DJANGO_DEBUG=0 без DJANGO_SECRET_KEY: задайте ключ в .env")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "market",
    "rates",
    "strategy",
    "charts",
    "radar",
    "analytics",
]

MIDDLEWARE = [
    # JSON графика — сотни килобайт цифр, сжатый в 5–7 раз меньше.
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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

# pgAdmin: создать базу spread_terminal и выдать пользователя.
# Пароль в репозиторий не кладём, только в окружение PyCharm.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "spread_terminal"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5433"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Сюда collectstatic складывает стили админки, отдаёт их nginx (deploy/nginx-spread.conf).
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Секрет Finam Trade API. В коде и в git его нет.
FINAM_API_SECRET = os.environ.get("FINAM_API_SECRET", "")
FINAM_BASE_URL = os.environ.get("FINAM_BASE_URL", "https://api.finam.ru")
# MOEX AlgoPack (data.moex.com). Пусто — радар работает без него и пишет, что AlgoPack не подключён.
ALGOPACK_TOKEN = os.environ.get("ALGOPACK_TOKEN", "")
ALGOPACK_BASE_URL = os.environ.get("ALGOPACK_BASE_URL", "https://apim.moex.com")

# SHIBOR — как в текущем strategies.py. RUSFARCNY подставим, если выберете его.
FOREIGN_RATE_SOURCE = os.environ.get("FOREIGN_RATE_SOURCE", "SHIBOR")
# Поддерживается только TIME_FRAME_H1: расписание, склейка и спред считаются по часу.
BAR_TIMEFRAME = os.environ.get("BAR_TIMEFRAME", "TIME_FRAME_H1")
