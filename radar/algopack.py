"""MOEX AlgoPack (data.moex.com): пока только проверка доступа.

FUTOI (позиции физлиц и юрлиц), Super Candles и HI2 подключаются следующим
этапом. Без токена радар работает и пишет, что AlgoPack не подключён.
"""

import httpx
from django.conf import settings

CHECK_EVERY = 30 * 60
PROBE = "/iss/analyticalproducts/futoi/securities/si.json"


def check():
    """(состояние, текст) для SourceStatus."""
    token = settings.ALGOPACK_TOKEN
    if not token:
        return "no_key", "нет ALGOPACK_TOKEN в .env"
    try:
        response = httpx.get(
            settings.ALGOPACK_BASE_URL.rstrip("/") + PROBE,
            headers={"Authorization": f"Bearer {token}"}, timeout=20.0,
        )
    except httpx.HTTPError as exc:
        return "error", f"нет связи с AlgoPack: {exc.__class__.__name__}"
    if response.status_code in (401, 403):
        return "error", f"токен AlgoPack не принят ({response.status_code})"
    if response.status_code >= 400:
        return "error", f"AlgoPack ответил {response.status_code}"
    return "ok", "токен принят; FUTOI, Super Candles и HI2 подключатся следующим этапом"
