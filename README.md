# CR − CNYRUBF

Живой график окна `spread_rate` из `strategies.py`. Сейчас готов только слой базы: часовые свечи, RUONIA и SHIBOR O/N. Графика ещё нет.

Время UTC. Спред позже будет CR − CNYRUBF.  
`spread_rate = raw − spot · (RUONIA − SHIBOR) / 100 · days / 365`.

## Как открыть

PyCharm: эта папка, Python 3.10+, зависимости из `requirements.txt`.

В pgAdmin создать базу `spread_terminal`. Секреты только в окружении проекта:

- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`
- `FINAM_API_SECRET` — секрет Finam Trade API, не JWT
- `BAR_TIMEFRAME` — `TIME_FRAME_H1`

Дальше:

```
python manage.py migrate
python manage.py sync_rates
python manage.py sync_bars
```

`sync_rates` без аргументов берёт дни с 2023-01-01 по сегодня.  
`sync_bars` без аргументов берёт последние 30 дней. Finam на часе глубже не отдаёт. Повторный запуск обновляет те же часы и не стирает уже накопленное, так что история начнёт копиться с первого запуска.

Свой файл SHIBOR вместо сайта:

```
python manage.py sync_rates --shibor-file /путь/SHIBOR.csv
```

Колонки как в текущем файле: `date`, `O/N`.

## Откуда цифры

| Ряд | Куда | Откуда |
|---|---|---|
| RUONIA | `rates_rateday.ruonia` | SOAP ЦБ `DailyInfo.Ruonia` |
| SHIBOR O/N | `rates_rateday.foreign_rate` | China Money `ShiborHis`, поле `ON` |
| CNYRUBF и CR | `market_candle` | Finam `GET /v1/instruments/{symbol}/bars` |

Символы Finam: `CNYRUBF@RTSX` и квартал вида `CRZ6@RTSX`. Склейка контракта — третья четверг, 20:50 UTC. Если Финам ответит 404, символ лежит в `market/finam.py` и в колонке `finam_symbol`.

Спот CNYRUB_TOM, фандинг и сам spread_rate в этот шаг не входят.

## Файлы загрузки

| Путь | Что делает |
|---|---|
| `rates/cbr.py` | разбор ответа ЦБ |
| `rates/foreign.py` | SHIBOR по годам и чтение CSV |
| `rates/management/commands/sync_rates.py` | запись в `RateDay` |
| `market/finam.py` | сессия и свечи |
| `market/roll.py` | какие CR попадают в интервал |
| `market/management/commands/sync_bars.py` | запись в `Candle` |
