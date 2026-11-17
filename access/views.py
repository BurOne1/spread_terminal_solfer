from django.conf import settings
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from access import groups


def _next(request):
    target = request.POST.get("next") or request.GET.get("next") or "/"
    return target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else "/"


def enter(request):
    """Приветственная страница: выбор группы. Уже выбрана — сразу на график."""
    if groups.group_of(request, fallback=False) and request.method == "GET":
        return redirect(_next(request))
    error = ""
    if request.method == "POST":
        group = request.POST.get("group", "")
        if group == groups.VIEWER:
            groups.enter(request, groups.VIEWER)
            return redirect("/")
        if group == groups.ANALYST:
            if not settings.ANALYST_PASSWORD:
                error = "Пароль отдела трейдинга не задан на сервере: ANALYST_PASSWORD в .env."
            elif groups.password_ok(request.POST.get("password", "")):
                groups.enter(request, groups.ANALYST)
                return redirect(_next(request))
            else:
                error = "Неверный пароль."
    return render(request, "access/enter.html", {
        "error": error, "next": _next(request), "analyst_ready": bool(settings.ANALYST_PASSWORD),
        "analyst_open": request.method == "POST" and request.POST.get("group") == groups.ANALYST,
    })


@require_POST
def leave(request):
    groups.leave(request)
    return redirect("/enter/")
