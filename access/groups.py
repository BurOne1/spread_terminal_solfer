"""Две группы без личных аккаунтов: «Отдел трейдинга» (с паролем) и «Просмотр» (без пароля).

Группа хранится в сессии Django (cookie на год, подписана сервером). У аналитика
в сессии ещё отпечаток пароля: сменили ANALYST_PASSWORD в .env — все аналитики
входят заново. Личные аккаунты Django остаются только для админки.
"""

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

ANALYST = "analyst"
VIEWER = "viewer"
GROUP_KEY = "group"
PRINT_KEY = "group_print"
TITLES = {ANALYST: "Отдел трейдинга", VIEWER: "Просмотр"}


def password_print():
    """Отпечаток пароля аналитиков: в сессии лежит он, а не сам пароль."""
    return salted_hmac("access.analyst", settings.ANALYST_PASSWORD or "").hexdigest()[:24]


def password_ok(password):
    expected = settings.ANALYST_PASSWORD or ""
    return bool(expected) and constant_time_compare(password or "", expected)


def group_of(request, fallback=True):
    """analyst, viewer или "" — группа не выбрана (или пароль аналитиков сменили).

    fallback — без выбранной группы взять ACCESS_DEFAULT_GROUP (тесты, локальный запуск).
    """
    session = getattr(request, "session", None)
    group = session.get(GROUP_KEY, "") if session is not None else ""
    if group == ANALYST and session.get(PRINT_KEY) != password_print():
        return ""
    if group in (ANALYST, VIEWER):
        return group
    # Тесты и локальный запуск без входа: группа по умолчанию из настроек.
    return (getattr(settings, "ACCESS_DEFAULT_GROUP", "") or "") if fallback else ""


def is_viewer(request):
    return group_of(request) == VIEWER


def enter(request, group):
    request.session.cycle_key()
    request.session[GROUP_KEY] = group
    if group == ANALYST:
        request.session[PRINT_KEY] = password_print()
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)


def leave(request):
    request.session.flush()
