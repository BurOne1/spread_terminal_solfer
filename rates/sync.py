"""Ежедневная догрузка RUONIA, SHIBOR, DFEDTARU и €STR. Чужие столбцы не затирает."""

from datetime import date, timedelta

from rates.cbr import fetch_ruonia
from rates.ecb import fetch_estr
from rates.foreign import fetch_shibor
from rates.fred import fetch_dfedtaru
from rates.models import RateDay


def sync_recent(days=40):
    end = date.today()
    start = end - timedelta(days=days)
    ruonia = fetch_ruonia(start, end)
    ruonia_n = _save(ruonia, "ruonia")
    shibor, shibor_n, note = [], 0, ""
    try:
        shibor = fetch_shibor(start, end)
        shibor_n = _save(shibor, "shibor")
    except Exception as exc:
        note = f"SHIBOR не ответил: {exc}"
    dfed, dfed_n = [], 0
    try:
        dfed = fetch_dfedtaru(date(2023, 1, 1), end)
        dfed_n = _save(dfed, "dfedtaru")
    except Exception as exc:
        extra = f"FRED не ответил, оставлена последняя ставка: {exc}"
        note = f"{note}; {extra}" if note else extra
    estr, estr_n = [], 0
    try:
        estr = fetch_estr(start, end)
        estr_n = _save(estr, "estr")
    except Exception as exc:
        extra = f"ЕЦБ не ответил, оставлена последняя €STR: {exc}"
        note = f"{note}; {extra}" if note else extra
    return {
        "ruonia_days": ruonia_n,
        "shibor_days": shibor_n,
        "ruonia": ruonia[-1] if ruonia else None,
        "shibor": shibor[-1] if shibor else _last("shibor"),
        "dfedtaru_days": dfed_n,
        "dfedtaru": dfed[-1] if dfed else _last("dfedtaru"),
        "estr_days": estr_n,
        "estr": estr[-1] if estr else _last("estr"),
        "note": note,
    }


def _last(field):
    return (
        RateDay.objects.filter(**{f"{field}__isnull": False})
        .order_by("-date")
        .values_list("date", field)
        .first()
    )


def _save(pairs, field, extra=None):
    extra = extra or {}
    for day, value in pairs:
        RateDay.objects.update_or_create(date=day, defaults={field: value, **extra})
    return len(pairs)