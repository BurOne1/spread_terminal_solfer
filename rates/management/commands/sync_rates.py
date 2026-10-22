"""Пишет RUONIA, SHIBOR, DFEDTARU и €STR в свои столбцы. Чужие поля не затирает."""

from datetime import date, datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from rates.cbr import fetch_ruonia
from rates.ecb import fetch_estr, read_estr_csv
from rates.foreign import fetch_shibor, read_shibor_csv
from rates.fred import fetch_dfedtaru, read_dfedtaru_csv
from rates.models import RateDay


class Command(BaseCommand):
    help = "Загружает RUONIA, SHIBOR, DFEDTARU и €STR в таблицу ставок."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", default="2023-01-01")
        parser.add_argument("--to", dest="end", default="")
        parser.add_argument("--shibor-file", default="", help="CSV с колонками date,O/N")
        parser.add_argument("--fred-file", default="", help="CSV DFEDTARU, если FRED не открывается")
        parser.add_argument("--estr-file", default="", help="CSV €STR, если ЕЦБ не открывается")

    def handle(self, *args, **options):
        start = date.fromisoformat(options["start"])
        end = date.fromisoformat(options["end"]) if options["end"] else datetime.now(timezone.utc).date()
        if end < start:
            raise CommandError("конец раньше начала")

        ruonia = fetch_ruonia(start, end)
        self.stdout.write(f"RUONIA {_save(ruonia, 'ruonia')} дней")

        if options["shibor_file"]:
            shibor = [(day, rate) for day, rate in read_shibor_csv(options["shibor_file"]) if start <= day <= end]
            origin = options["shibor_file"]
        else:
            shibor = fetch_shibor(start, end)
            origin = "China Money"
        self.stdout.write(f"SHIBOR {_save(shibor, 'shibor')} дней, источник {origin}")

        if options["fred_file"]:
            foreign = read_dfedtaru_csv(options["fred_file"], start, end)
            origin = options["fred_file"]
            self.stdout.write(f"DFEDTARU {_save(foreign, 'dfedtaru')} дней, источник {origin}")
        else:
            try:
                foreign = fetch_dfedtaru(start, end)
                self.stdout.write(f"DFEDTARU {_save(foreign, 'dfedtaru')} дней, источник FRED")
            except Exception as exc:
                self.stderr.write(
                    f"FRED не ответил: {exc}. Старые DFEDTARU оставлены, €STR загружается дальше."
                )

        if options["estr_file"]:
            estr = read_estr_csv(options["estr_file"], start, end)
            origin = options["estr_file"]
        else:
            try:
                estr = fetch_estr(start, end)
                origin = "ECB"
            except Exception as exc:
                raise CommandError(
                    f"ЕЦБ не ответил: {exc}. Скачайте CSV и запустите с --estr-file"
                ) from exc
        self.stdout.write(f"ESTR {_save(estr, 'estr')} дней, источник {origin}")


def _save(pairs, field, extra=None):
    extra = extra or {}
    for day, value in pairs:
        RateDay.objects.update_or_create(date=day, defaults={field: value, **extra})
    return len(pairs)
