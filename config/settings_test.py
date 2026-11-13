"""Тесты без Postgres и секретов: SQLite в памяти.

python manage.py test --settings=config.settings_test
"""

from config.settings import *  # noqa: F401,F403

SECRET_KEY = "test"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
FINAM_API_SECRET = ""

# Кэш /api/spread/ общий на процесс: в тестах ответ переиспользуется только при тех же барах.
SPREAD_MIN_AGE = 0
