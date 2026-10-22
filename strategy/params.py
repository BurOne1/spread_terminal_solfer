"""Параметры CNY из strategies.py. Меняются здесь, не в движке."""

NOTIONAL = 1000

LONG_LEVELS = (-0.7, -0.20, -0.27)
SHORT_LEVELS = (0.7, 0.20, 0.27)
LONG_TP = (-0.03, 0.0, 0.0)
SHORT_TP = (0.03, 0.0, -0.0)

EXIT_ON_SPREAD_SIGN = False
REQUIRE_LONG_DIFF_SIGN = True
TOUCH_ENTRY = True
BLOCK_AFTER_ZERO = False
NO_TRADE_BARS = 26
MIN_PRICE_SPREAD = 0.1

ROLL_TIME = (20, 50)  # UTC. В комментарии скрипта написано 18:50 МСК — это другое время.
MONTH_CODE = {"H": 3, "M": 6, "U": 9, "Z": 12}

COMM_EXCHANGE_PCT = 0.00462
COMM_BROKER_KOP = 0.19

PERP_CODE = "CNYRUBF"
SPOT_CODE = "CNYRUB_TOM"
FUTURE_ROOT = "CR"
