"""Каждый запрос — через группу. Зрителю открыто только нужное для графика спреда.

Проверка на сервере, а не скрытием кнопок: адрес /analytics/ или /api/radar/,
набранный руками, зрителю тоже закрыт. Пары зрителя проверяют сами виды
(charts.views: viewer_pairs).
"""

from django.http import JsonResponse
from django.shortcuts import redirect

from access.groups import ANALYST, VIEWER, group_of

# Без выбора группы: вход, статика, админка (у неё свой вход суперпользователя).
OPEN = ("/enter/", "/static/", "/admin/", "/favicon.ico")
# Зрителю: график, его данные, список пар, опубликованные каналы (только чтение), выход.
VIEWER_PATHS = {"/", "/api/spread/", "/api/watchlist/", "/api/published/", "/leave/"}
VIEWER_READ_ONLY = {"/api/published/"}


class GroupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith(OPEN):
            return self.get_response(request)
        group = group_of(request)
        request.group = group
        if group not in (ANALYST, VIEWER):
            if path.startswith("/api/"):
                return JsonResponse({"error": "выберите группу на странице входа"}, status=401)
            return redirect(f"/enter/?next={path}")
        if group == VIEWER:
            allowed = path in VIEWER_PATHS and not (path in VIEWER_READ_ONLY and request.method != "GET")
            if not allowed:
                if path.startswith("/api/"):
                    return JsonResponse({"error": "недоступно группе «Просмотр»"}, status=403)
                return redirect("/")
        return self.get_response(request)
