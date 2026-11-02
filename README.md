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
| SICAL, EUCAL, CRCAL | секонд − фронт Si/Eu/CR | как у USD/EUR/CNY |
| GDCAL, SVCAL | секонд − фронт GD/SV, доллары | ставка ФРС, рублёвой нет |
| TRIGD2, TRIGL2 | TRIGD и TRIGL на вторых кварталах | RUONIA + ставка ФРС |

Лоты: валюта 1000, золото 1 г, серебро 100 г, TRIGD 1 унция, TRISV 10 унций.

Календарь: `raw = секонд − фронт`, `fair = спот · (r − r_foreign) / 100 · (t секонда − t фронта) / 365`.
Окно и склейка — фронта: в третий четверг декабря после склейки SiZ2026 пара
SiZ2026/SiH2027 сменяется на SiH2027/SiM2027. Своих инструментов у календарей
и секондов нет, поле `base` пары говорит, чьи кварталы брать. Фандинга у календаря нет.

## Запуск

`.env` в корне: `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `FINAM_API_SECRET`
(и при необходимости `POSTGRES_DB/USER/HOST/PORT`).

```
python manage.py migrate
python manage.py add_eur | add_gold | add_silver | add_tri | add_btc   # нужные пары
python manage.py add_seconds        # календари и треугольники на секондах, после базовых пар
python manage.py sync_rates
python manage.py sync_funding
python manage.py sync_bars          # Finam отдаёт час примерно за 30 дней
python manage.py build_spread
python manage.py runserver
```

Постоянно работают `run_live` (Finam) и, для крипты, `run_bybit`.
`run_live` копит котировки и бары в памяти и раз в 5 с пишет их пачкой,
затем пересчитывает хвост spread_rate у пар, где что-то поменялось
(календари и треугольники — вслед за своими ногами). Раз в сутки обновляет
ставки и фандинг. Символы фронта и секонда пересчитываются при каждом
переоткрытии сокета (раз в 11 минут), недостающий квартал заводится сам.
Пары USD в командах нет: её надо завести вручную, `fix_usd` правит спот и масштаб.

## График

`/` — спред, индикатор кэрри и PnL стратегии. Уровни, режим входа, усреднение
и цена часа (закрытие, открытие, (O+C)/2) хранятся в браузере у каждого пользователя
и уходят параметрами в `/api/spread/`. Стратегия считается на лету и в базу не пишет.
Без своих уровней берутся сохранённые у пары.
Ответ сжимается gzip, а без новых баров отдаётся как 304 по ETag;
скрытая вкладка сервер не опрашивает.

`run_strategy --pair CNY` — то же в консоли с уровнями пары: закрытые сделки
пишет в `PaperTrade`, открытую позицию держит в `PositionState`.

## Тесты

```
python manage.py test --settings=config.settings_test
```

SQLite в памяти, Postgres и секреты не нужны.
