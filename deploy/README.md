# Развёртывание на ВМ (Ubuntu 24.04)

Схема: nginx (порт 80) → gunicorn (127.0.0.1:8000) → Django. Рядом служба
`spread-live` (`run_live`: свечи Finam, пересчёт спредов, ставки и фандинг
раз в сутки). Радар (`run_radar`) пока не запускаем — шаг «Позже» в конце.
База — PostgreSQL на той же ВМ, новая: история копится с нуля.

Всё работает от вашего пользователя Linux (того, под которым вы заходите по
SSH), отдельного пользователя не создаём. Код — в `/opt/spread_terminal`;
другой путь — поправьте в `*.service` и в `nginx-spread.conf`.

Память на 2 ГБ: PostgreSQL ~150 МБ, gunicorn ~150 МБ, run_live ~80 МБ —
около 0,4 ГБ сверху того, что уже занято. Swap — страховка (шаг 1).

## 1. Пакеты и swap

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev postgresql nginx git
```

Swap — файл на диске, куда система выгружает редко нужную память, когда
оперативная кончается. Без него при нехватке памяти ядро убивает процесс
(обычно самый большой — PostgreSQL или ваш второй скрипт). Со swap система
просто замедлится. На 2 ГБ с двумя проектами — дешёвая страховка: 2 ГБ диска.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

Если `free -h` уже показывает строку Swap с ненулевым размером — шаг не нужен.

## 2. Код

Сначала закоммитьте и отправьте изменения на GitHub с рабочего компьютера.

```bash
sudo mkdir -p /opt/spread_terminal && sudo chown $USER:$USER /opt/spread_terminal
git clone https://github.com/BurOne1/spread_terminal_solfer.git /opt/spread_terminal
cd /opt/spread_terminal
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Дальше все команды — из папки `/opt/spread_terminal`.

Репозиторий приватный — git попросит логин и токен GitHub (Settings →
Developer settings → Personal access tokens, права на чтение репозитория).

## 3. База

`spread` здесь — пользователь базы данных, не Linux: под ним Django ходит в PostgreSQL.

```bash
sudo -u postgres psql -c "CREATE USER spread WITH PASSWORD 'придумайте-пароль';"
sudo -u postgres psql -c "CREATE DATABASE spread_terminal OWNER spread;"
```

## 4. Настройки `.env`

`nano /opt/spread_terminal/.env` — все строки обязательны:

```
DJANGO_SECRET_KEY=длинная-случайная-строка
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=172.16.207.66
POSTGRES_USER=spread
POSTGRES_PASSWORD=пароль-из-шага-3
POSTGRES_PORT=5432
FINAM_API_SECRET=...
```

- `DJANGO_SECRET_KEY` — `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- `DJANGO_ALLOWED_HOSTS` — без адреса ВМ сайт ответит «400 Bad Request».
- `POSTGRES_PORT=5432` — по умолчанию в проекте 5433, как на Windows.
- `DJANGO_DEBUG=0` — иначе при ошибке страница покажет настройки сервера.
- Если откроете сайт по имени и https — добавьте имя в `DJANGO_ALLOWED_HOSTS`
  и строку `DJANGO_CSRF_TRUSTED_ORIGINS=https://имя` (без неё не войти в админку).

```bash
chmod 600 .env
venv/bin/python manage.py migrate
venv/bin/python manage.py collectstatic --noinput
venv/bin/python manage.py createsuperuser
```

`chmod 600` — файл с ключами читаете только вы. `migrate` создаёт таблицы,
`collectstatic` складывает стили админки в `staticfiles/` для nginx,
`createsuperuser` — логин и пароль для `/admin/`.

## 5. Пары и первые данные

База пустая, кроме пары CNY: её создаёт миграция, но без контрактов — их
заводит `ensure_contracts --pair CNY`. Порядок важен: календари, треугольники и
кросс-пары берут ноги у CNY, USD, GOLD, EUR.

```bash
M="venv/bin/python manage.py"
$M ensure_contracts --pair CNY
$M add_usd
$M add_eur
$M add_gold
$M add_silver
$M add_tri
$M add_seconds
$M add_brent
$M add_extra_spreads
$M sync_rates
$M sync_funding
$M sync_bars
$M build_spread
```

- `sync_rates` берёт RUONIA, SHIBOR, ставку ФРС и €STR. Если сайт ФРС (FRED)
  с ВМ не открывается — `$M sync_rates --fred-file DFEDTARU.csv` (файл в репозитории).
- `sync_bars` загрузит часовые свечи за последние ~30 дней — больше Finam
  не отдаёт. Дальше история копится сама, пока работает `spread-live`.
- Пара BTC (Bybit) — `add_btc`, `sync_bybit` и служба `spread-bybit` (как в шаге 6), если нужна.

## 6. Службы

Службы systemd держат сайт и `run_live` в фоне без открытого терминала,
запускают их после перезагрузки ВМ и перезапускают при падении. В файлах
служб вместо `__USER__` подставляется ваш пользователь:

```bash
for name in spread-web spread-live; do
  sed "s/__USER__/$USER/" deploy/$name.service | sudo tee /etc/systemd/system/$name.service > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now spread-web spread-live
systemctl status spread-web spread-live
```

Логи: `journalctl -u spread-live -f` (Ctrl+C — выйти), `journalctl -u spread-web -n 50`.
На Windows `run_live` после этого остановите: два потока на одном ключе Finam
только тратят его лимиты.

## 7. nginx

Сначала посмотрите, что nginx уже обслуживает:

```bash
ls -l /etc/nginx/sites-enabled/
```

- Только `default`, и `http://172.16.207.66/` показывает «Welcome to nginx» (или
  nginx до этого не стоял) — делайте всё как ниже, `rm` уберёт лишь эту заглушку.
- Есть конфиги другого проекта или он настроен в `default` — команду `rm` не
  выполняйте, а в `deploy/nginx-spread.conf` замените `listen 80;` на `listen 8080;`.

```bash
sudo cp deploy/nginx-spread.conf /etc/nginx/sites-available/spread
sudo ln -s /etc/nginx/sites-available/spread /etc/nginx/sites-enabled/spread
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Сайт: `http://172.16.207.66/`, админка — `/admin/`. Если порт 80 на ВМ уже
занят (`sudo ss -ltnp | grep ':80 '`) — поменяйте `listen 80` в конфиге,
например на `listen 8080`, и открывайте `http://172.16.207.66:8080/`.

## HTTPS

Копирование снимка графика в буфер браузеры разрешают только по https или на
localhost (по http снимок скачивается файлом). Варианты:

- **SSH-туннель** — ничего не открывать наружу:
  `ssh -L 8000:127.0.0.1:8000 user@172.16.207.66`, затем `http://localhost:8000/`.
- **Свой сертификат во внутренней сети:** `mkcert` выпускает сертификат на
  `172.16.207.66` и добавляет корень в доверенные на вашем компьютере; в nginx —
  `listen 443 ssl` с этими файлами.
- **Домен и Let's Encrypt** — если ВМ доступна из интернета: `sudo apt install
  certbot python3-certbot-nginx`, `sudo certbot --nginx -d имя`.

## Запуск и остановка

```bash
sudo systemctl stop spread-web spread-live      # остановить
sudo systemctl start spread-web spread-live     # запустить
sudo systemctl restart spread-web spread-live   # перезапустить
systemctl status spread-web spread-live         # работают ли
sudo systemctl disable spread-web spread-live   # не запускать после перезагрузки (enable — вернуть)
```

Одну службу — по имени: `sudo systemctl stop spread-web`. Пока `spread-web`
остановлен, сайт отвечает «502». nginx не трогаем: он общий.

## Обновление

```bash
cd /opt/spread_terminal
git pull
venv/bin/pip install -r requirements.txt
venv/bin/python manage.py migrate
venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart spread-web spread-live
```

Если поменялись файлы `deploy/*.service` — повторите шаг 6.

## Позже: радар

```bash
sed "s/__USER__/$USER/" deploy/spread-radar.service | sudo tee /etc/systemd/system/spread-radar.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now spread-radar
```

После этого — `spread-radar` и в `systemctl restart` раздела «Обновление».
Без службы страница `/radar/` открывается и пишет, что run_radar не запущен.

## Безопасность

- `DJANGO_DEBUG=0` на сервере всегда: иначе ошибки показывают настройки.
- Наружу открыт только nginx; gunicorn и PostgreSQL слушают 127.0.0.1.
  Если ВМ видна не только из внутренней сети:
  `sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable`.
- Админка по http передаёт пароль открытым текстом — во внутренней сети
  терпимо, снаружи — только по https.
