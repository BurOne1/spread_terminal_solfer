"""Проверка Телеграма: тестовое сообщение, --chats — номера чатов бота, --check — каналы сейчас.

Токен берётся из .env и никуда не печатается.
"""

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from charts.alerts import send


class Command(BaseCommand):
    help = "Отправляет тестовое сообщение в TELEGRAM_CHAT_ID; --chats — найти номер чата; --check — проверить каналы."

    def add_arguments(self, parser):
        parser.add_argument("--chats", action="store_true", help="показать чаты из последних сообщений боту (getUpdates)")
        parser.add_argument("--check", action="store_true", help="проверить опубликованные каналы и отправить события")

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stdout.write("в .env нет TELEGRAM_BOT_TOKEN")
            return
        if options["chats"]:
            self.chats(token)
            return
        if not settings.TELEGRAM_CHAT_ID:
            self.stdout.write("в .env нет TELEGRAM_CHAT_ID — найдите его: python manage.py telegram_test --chats")
            return
        ok = send("Спреды: проверка связи. Оповещения по каналам будут приходить сюда.", self.stdout.write)
        self.stdout.write("сообщение отправлено" if ok else "не отправлено, см. выше")
        if options["check"]:
            from charts.alerts import check_channels
            from charts.models import PublishedChannel
            sent = check_channels(log=self.stdout.write)
            for item in PublishedChannel.objects.select_related("pair"):
                self.stdout.write(f"  {item.pair.code} канал {item.drawing_id}: зона {item.zone or '—'}")
            self.stdout.write(f"событий отправлено: {len(sent)}")

    def chats(self, token):
        base = f"https://api.telegram.org/bot{token}"
        try:
            me = httpx.get(f"{base}/getMe", timeout=10.0)
            if me.status_code != 200:
                self.stdout.write(f"токен не принят ({me.status_code}): проверьте TELEGRAM_BOT_TOKEN в .env — "
                                  "одна строка, без кавычек, пробелов и угловых скобок")
                return
            name = me.json().get("result", {}).get("username", "")
            self.stdout.write(f"бот @{name}, токен верный")
            updates = httpx.get(f"{base}/getUpdates", timeout=10.0)
        except httpx.HTTPError as exc:
            self.stdout.write(f"телеграм недоступен: {type(exc).__name__}")
            return
        body = updates.json() if updates.headers.get("content-type", "").startswith("application/json") else {}
        if updates.status_code == 409:
            self.stdout.write("у бота включён webhook — getUpdates не работает. Отключите: откройте "
                              "deleteWebhook или напишите сюда, добавлю команду")
            return
        found = {}
        for update in body.get("result", []):
            for key in ("message", "channel_post", "my_chat_member", "edited_message"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    found[chat["id"]] = f"{chat.get('type', '')} «{chat.get('title') or chat.get('username') or chat.get('first_name', '')}»"
        if not found:
            self.stdout.write(f"сообщений боту пока нет. Напишите в группе /start@{name} (или упомяните @{name}) "
                              "и запустите команду снова")
            return
        for chat_id, title in found.items():
            self.stdout.write(f"  TELEGRAM_CHAT_ID={chat_id}   {title}")
