"""Правила сигналов радара: пороги, окна и паузы, общие и для инструмента.

Правило — строка SignalRule: вид сигнала и охват. Пустой охват — для всех
инструментов, корень квартала (Si) — для всех его контрактов, символ
(SiZ6@RTSX) — для одного. Более узкое правило переписывает только
заполненные поля, пустые берёт у более широкого, а то — у DEFAULTS.
"""

import re

# Значения по умолчанию: ровно то, что детекторы делали до правил.
DEFAULTS = {
    "large": {"enabled": True, "threshold": 99.5, "ratio": 3.0, "cooldown_seconds": 0},
    "sweep": {"enabled": True, "threshold": 99.0, "levels": 3, "gap_ms": 100, "cooldown_seconds": 20},
    "wall": {"enabled": True, "threshold": 99.5, "ratio": 5.0, "confirm_seconds": 30, "cooldown_seconds": 60},
    "pulled": {"enabled": True, "levels": 3, "cooldown_seconds": 60},
    "eaten": {"enabled": True, "ratio": 0.5, "cooldown_seconds": 60},
    "volume": {"enabled": True, "threshold": 5.0, "ratio": 3.0, "window_minutes": 1, "cooldown_seconds": 60},
    "oi": {"enabled": True, "threshold": 6.0, "window_minutes": 1, "cooldown_seconds": 60},
}

# Что значит каждое поле у каждого сигнала: подпись и подсказка в админке.
FIELDS = {
    "large": {
        "threshold": ("Процентиль размера заявки", "Заявка крупнее этого процентиля заявок инструмента. 99,5 — одна из двухсот."),
        "ratio": ("Во сколько раз больше медианы", "И при этом не меньше стольких медианных заявок."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд на сторону. 0 — без паузы."),
    },
    "sweep": {
        "threshold": ("Процентиль размера заявки", "Проход засчитывается только для заявок крупнее этого процентиля."),
        "levels": ("Уровней стакана, не меньше", "Сколько разных цен должна пройти одна заявка."),
        "gap_ms": ("Сделки одной заявки, мс", "Сделки одной стороны с промежутком не больше этого считаются одной заявкой. "
                   "Действует и на крупные сделки."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд на сторону."),
    },
    "wall": {
        "threshold": ("Процентиль уровней стакана", "Уровень крупнее этого процентиля уровней инструмента за последние часы."),
        "ratio": ("Во сколько раз больше медианы", "И во столько раз больше медианного уровня стакана сейчас."),
        "confirm_seconds": ("Простоять, с", "Стена объявляется, только если простояла столько. Перестановки короче не в счёт."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд на сторону."),
    },
    "pulled": {
        "levels": ("Уровней от лучшей цены, не дальше", "Снятие засчитывается, только если стена была так близко к цене."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд на сторону."),
    },
    "eaten": {
        "ratio": ("Доля исполнения", "Исполнилось хотя бы столько от ушедшего объёма стены — съедена, иначе снята. 0,5 — половина."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд на сторону."),
    },
    "volume": {
        "threshold": ("z-оценка, не меньше", "Во сколько типичных отклонений объём окна выше нормы."),
        "ratio": ("Во сколько раз больше нормы", "И не меньше стольких медианных объёмов окна."),
        "window_minutes": ("Окно, минут", "Объём за столько последних минут против такого же окна раньше. "
                           "1 — минута против той же минуты прошлых дней."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд; не меньше длины окна."),
    },
    "oi": {
        "threshold": ("z-оценка, не меньше", "Во сколько типичных отклонений изменение ОИ за окно больше обычного."),
        "window_minutes": ("Окно, минут", "Изменение открытого интереса за столько последних минут."),
        "cooldown_seconds": ("Пауза, с", "Не чаще раза в столько секунд; не меньше длины окна."),
    },
}

PARAMS = ("threshold", "ratio", "levels", "gap_ms", "confirm_seconds", "window_minutes", "cooldown_seconds")


def matches(symbol, scope):
    """Подходит ли охват символу: пусто — всем, с @ — один символ, иначе корень квартала."""
    if not scope:
        return True
    if "@" in scope:
        return symbol.lower() == scope.lower()
    ticker = symbol.split("@")[0]
    return re.fullmatch(re.escape(scope) + r"[FGHJKMNQUVXZ]\d{1,2}", ticker) is not None


def _rank(scope):
    return 0 if not scope else 2 if "@" in scope else 1


def resolve(symbol, rows):
    """{вид: параметры} для символа. rows — строки SignalRule или словари с теми же полями."""
    found = {kind: dict(values) for kind, values in DEFAULTS.items()}
    fitting = [row for row in rows if _get(row, "kind") in found and matches(symbol, _get(row, "scope") or "")]
    for row in sorted(fitting, key=lambda row: _rank(_get(row, "scope") or "")):
        values = found[_get(row, "kind")]
        values["enabled"] = bool(_get(row, "enabled"))
        for name in PARAMS:
            value = _get(row, name)
            if value is not None and name in DEFAULTS[_get(row, "kind")]:
                values[name] = value
    return found


def _get(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name)
