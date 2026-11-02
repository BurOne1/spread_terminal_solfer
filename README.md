# spread_terminal

Терминал для анализа спредов квартальный фьючерс − вечный в реальном времени
и бумажного прогона стратегии по spread_rate. Заявок не выставляет.
Django + PostgreSQL, данные Finam Trade API, Мосбиржи, ЦБ и Bybit. Время везде UTC,
таймфрейм только час (`TIME_FRAME_H1`).

`spread_rate = raw − спот · (RUONIA − иностранная ставка) / 100 · дни / 365`,
где `raw = квартал − вечный`.

## Пары

| Пара | Ноги | Иностранная ставка |
|---|---|---|
| CNY, USD, EUR | CR/Si/Eu − CNYRUBF/USDRUBF/EURRUBF | SHIBOR, DFEDTARU, €STR |
| GOLD, SILVER | GL/S2 − GLDRUBF/SLVRUBF, цена за грамм | нет, только RUONIA |
| TRIGD, TRISV, TRIGL | GD·SI − GLDRUBF, SV·SI − SLVRUBF, GD·SI − GL | RUONIA + ставка ФРС |
| BTC (Bybit) | квартал − вечный BTCUSDT | EWMA14 8-часового фандинга |

Лоты: валюта 1000, золото 1 г, серебро 100 г, TRIGD 1 унция, TRISV 10 унций.

## Запуск

`.env` в корне: `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `FINAM_API_SECRET`
(и при необходимости `POSTGRES_DB/USER/HOST/PORT`).

```
python manage.py migrate
python manage.py add_eur | add_gold | add_silver | add_tri | add_btc   # нужные пары
python manage.py sync_rates
python manage.py sync_funding
python manage.py sync_bars          # Finam отдаёт час примерно за 30 дней
python manage.py build_spread
python manage.py runserver
```

Постоянно работают `run_live` (Finam) и, для крипты, `run_bybit`.
`run_live` пишет свечи, раз в 5 с пересчитывает хвост spread_rate у пар,
где что-то поменялось, раз в сутки обновляет ставки и фандинг.
Пары USD в командах нет: её надо завести вручную, `fix_usd` правит спот и масштаб.

## График

`/` — спред, индикатор кэрри и PnL стратегии. Уровни, режим входа, усреднение
и цена часа (закрытие, открытие, (O+C)/2) хранятся в браузере у каждого пользователя
и уходят параметрами в `/api/spread/`. Стратегия считается на лету и в базу не пишет.
Без своих уровней берутся сохранённые у пары.

`run_strategy --pair CNY` — то же в консоли с уровнями пары: закрытые сделки
пишет в `PaperTrade`, открытую позицию держит в `PositionState`.

## Тесты

```
python manage.py test --settings=config.settings_test
```

SQLite в памяти, Postgres и секреты не нужны.
