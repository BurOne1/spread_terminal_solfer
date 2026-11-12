"""Статистика контрактов спреда по дням до экспирации — по мотивам gas_spreads.py.

Матрица S: строка — контракт (год), столбец d — дней до склейки окна пары,
0 … width−1. P — цена, от которой считается капитал (спот бара). Нет данных — nan.

Три сетки «день входа × день выхода» (вход дальше от экспирации, чем выход):
- returns_grid — доход сделки: сторона по знаку среднего изменения,
  % капитала за период, годовые, худший год, доля прибыльных лет, MAE;
- move_grid — движение к нулю: |спред| каждого контракта в своём масштабе,
  + — сузился, − — разошёлся;
- side_grid — куда шёл спред: тот же масштаб, но со знаком, + — вырос (лонг),
  − — упал (шорт).

Без numpy: сетки до ~100 × 100 дней и пары десятков контрактов считаются
за доли секунды, а на ВМ не нужна лишняя зависимость.
"""

import math
import statistics

NAN = float("nan")


def finite(value):
    return value is not None and value == value and not math.isinf(value)


def mean(values):
    values = [value for value in values if finite(value)]
    return sum(values) / len(values) if values else NAN


def median(values):
    values = [value for value in values if finite(value)]
    return statistics.median(values) if values else NAN


def stdev(values):
    values = [value for value in values if finite(value)]
    return statistics.stdev(values) if len(values) > 1 else NAN


def blank(width):
    return [[NAN] * width for _ in range(width)]


def _roll(row, half):
    out = []
    for index in range(len(row)):
        window = [value for value in row[max(0, index - half):index + half + 1] if finite(value)]
        out.append(sum(window) / len(window) if window else NAN)
    return out


def smooth2d(grid, half):
    """Скользящее среднее ±half по обеим осям; пустые клетки остаются пустыми."""
    if half <= 0:
        return [row[:] for row in grid]
    rows = [_roll(row, half) for row in grid]
    columns = [_roll(list(column), half) for column in zip(*rows)]
    out = [list(row) for row in zip(*columns)]
    for din, row in enumerate(grid):
        for dout, value in enumerate(row):
            if not finite(value):
                out[din][dout] = NAN
    return out


def value_at(points, day, tolerance=3):
    """Значение в ближайший к day день не дальше tolerance; points — {день: значение}."""
    if day in points:
        return points[day]
    for step in range(1, tolerance + 1):
        for candidate in (day - step, day + step):
            if candidate in points:
                return points[candidate]
    return NAN


def matrix(contracts, width, tolerance=3):
    """Контракты с дневными точками → S и P размером «контракт × день 0 … width−1»."""
    S, P = [], []
    for contract in contracts:
        values = {day: value for day, value, _price in contract["points"]}
        prices = {day: price for day, _value, price in contract["points"]}
        S.append([value_at(values, day, tolerance) for day in range(width)])
        P.append([value_at(prices, day, tolerance) for day in range(width)])
    return S, P


def _cells(width, min_hold):
    for din in range(min_hold, width):
        for dout in range(0, din - min_hold + 1):
            yield din, dout


def returns_grid(S, P, min_hold=10, capital_pct=100.0, cost=0.0, min_years=2, smooth_half=5, metric="ret"):
    """Доход сделки «вход за din дней → выход за dout дней» по фактическому спреду на выходе.

    Сторона клетки — по знаку суммы изменений: спред падал — продаём (+1),
    рос — покупаем (−1). r = (сторона × (s_вход − s_выход) − cost) / капитал × 100,
    капитал — capital_pct % цены в день входа. Годовые — r × 365 / дней удержания.
    MAE — худшее движение против позиции на пути, % капитала, максимум по годам.
    """
    width = len(S[0]) if S else 0
    keys = ("ret", "ann", "worst", "win", "sd", "mae", "n", "side")
    G = {key: blank(width) for key in keys}
    for din in range(min_hold, width):
        # Наибольший и наименьший спред от выхода до входа: копим от din назад.
        run_max, run_min = [], []
        for row in S:
            highs, lows = [NAN] * (din + 1), [NAN] * (din + 1)
            high, low = -math.inf, math.inf
            for dout in range(din, -1, -1):
                if finite(row[dout]):
                    high, low = max(high, row[dout]), min(low, row[dout])
                highs[dout], lows[dout] = high, low
            run_max.append(highs)
            run_min.append(lows)
        for dout in range(0, din - min_hold + 1):
            rows = []
            for index, row in enumerate(S):
                s_in, s_out, price = row[din], row[dout], P[index][din]
                if finite(s_in) and finite(s_out) and finite(price) and price > 0:
                    rows.append((index, s_in, s_in - s_out, price * capital_pct / 100.0))
            if len(rows) < min_years:
                continue
            side = 1 if sum(change for _i, _s, change, _c in rows) >= 0 else -1
            hold = din - dout
            returns, annual, adverse = [], [], []
            for index, s_in, change, capital in rows:
                result = (side * change - cost) / capital * 100.0
                returns.append(result)
                annual.append(result * 365.0 / hold)
                against = run_max[index][dout] - s_in if side > 0 else s_in - run_min[index][dout]
                adverse.append(max(against, 0.0) / capital * 100.0)
            G["ret"][din][dout] = mean(returns)
            G["ann"][din][dout] = mean(annual)
            G["worst"][din][dout] = min(annual)
            G["win"][din][dout] = sum(value > 0 for value in returns) / len(returns)
            G["sd"][din][dout] = stdev(returns)
            G["mae"][din][dout] = max(adverse)
            G["n"][din][dout] = len(returns)
            G["side"][din][dout] = side
    G["ratio"] = [
        [ret / sd if finite(ret) and finite(sd) and sd > 0 else NAN for ret, sd in zip(row_ret, row_sd)]
        for row_ret, row_sd in zip(G["ret"], G["sd"])
    ]
    G["smooth"] = smooth2d(G[metric if metric in G else "ret"], smooth_half)
    score = [
        [value if finite(value) and finite(G["ret"][din][dout]) and G["ret"][din][dout] > 0 else NAN
         for dout, value in enumerate(row)]
        for din, row in enumerate(G["smooth"])
    ]
    return G, _argmax(score)


def _scales(S, floor_share):
    """Типичный масштаб контракта — медиана |спреда|, не меньше floor_share медианы по контрактам."""
    raw = [median([abs(value) for value in row if finite(value)]) for row in S]
    finite_raw = [value for value in raw if finite(value)]
    floor = median(finite_raw) * floor_share if finite_raw else 1.0
    return [max(value, floor) if finite(value) and max(value, floor) > 0 else NAN for value in raw]


def _normalized_grid(S, change, min_hold, min_years, smooth_half):
    width = len(S[0]) if S else 0
    M = {key: blank(width) for key in ("move", "agree", "raw", "n")}
    for din, dout in _cells(width, min_hold):
        moves, raws = [], []
        for normalized, absolute in change(din, dout):
            if finite(normalized):
                moves.append(normalized)
                raws.append(absolute)
        if len(moves) < min_years:
            continue
        middle = median(moves)
        M["move"][din][dout] = middle
        same = sum((value > 0) == (middle > 0) for value in moves if value != 0)
        M["agree"][din][dout] = same / len(moves)
        M["raw"][din][dout] = median(raws)
        M["n"][din][dout] = len(moves)
    M["smooth"] = smooth2d(M["move"], smooth_half)
    return M


def move_grid(S, min_hold=10, min_years=2, smooth_half=5, floor_share=0.2):
    """Сужение |спреда| к нулю: |s_вход| − |s_выход| в масштабе контракта, медиана по годам."""
    scales = _scales(S, floor_share)

    def change(din, dout):
        for row, scale in zip(S, scales):
            if finite(row[din]) and finite(row[dout]) and finite(scale):
                narrowing = abs(row[din]) - abs(row[dout])
                yield narrowing / scale, narrowing

    M = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    score = [[value if finite(value) and finite(M["move"][din][dout]) and M["move"][din][dout] > 0 else NAN
              for dout, value in enumerate(row)] for din, row in enumerate(M["smooth"])]
    return M, _argmax(score)


def side_grid(S, min_hold=10, min_years=2, smooth_half=5, floor_share=0.2):
    """Куда шёл спред: s_выход − s_вход в масштабе контракта. + — рос (лонг), − — падал (шорт).

    Лучшая клетка — наибольшее по модулю сглаженное движение, где не меньше
    половины лет согласны со знаком медианы.
    """
    scales = _scales(S, floor_share)

    def change(din, dout):
        for row, scale in zip(S, scales):
            if finite(row[din]) and finite(row[dout]) and finite(scale):
                move = row[dout] - row[din]
                yield move / scale, move

    M = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    score = [[abs(value) if finite(value) and finite(M["agree"][din][dout]) and M["agree"][din][dout] >= 0.5 else NAN
              for dout, value in enumerate(row)] for din, row in enumerate(M["smooth"])]
    return M, _argmax(score)


def _argmax(grid):
    best, where = -math.inf, None
    for din, row in enumerate(grid):
        for dout, value in enumerate(row):
            if finite(value) and value > best:
                best, where = value, (din, dout)
    return where


def best_ranges(G, top=5, gap=5):
    """Несколько лучших клеток доходности, не ближе gap дней друг к другу по входу и выходу."""
    cells = sorted(
        ((value, din, dout) for din, row in enumerate(G["smooth"]) for dout, value in enumerate(row)
         if finite(value) and finite(G["ret"][din][dout]) and G["ret"][din][dout] > 0),
        reverse=True,
    )
    picked = []
    for _value, din, dout in cells:
        if all(abs(din - a) > gap or abs(dout - b) > gap for a, b in picked):
            picked.append((din, dout))
        if len(picked) == top:
            break
    return [
        {
            "entry": din, "exit": dout, "hold": din - dout,
            "side": "продажа" if G["side"][din][dout] > 0 else "покупка",
            "ret": G["ret"][din][dout], "ann": G["ann"][din][dout], "worst": G["worst"][din][dout],
            "win": G["win"][din][dout], "mae": G["mae"][din][dout], "ratio": G["ratio"][din][dout],
            "n": G["n"][din][dout],
        }
        for din, dout in picked
    ]


def clean(value, digits=5):
    """nan и бесконечности → None для JSON, числа — с ограниченной точностью."""
    if isinstance(value, list):
        return [clean(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: clean(item, digits) for key, item in value.items()}
    if isinstance(value, tuple):
        return [clean(item, digits) for item in value]
    if isinstance(value, float):
        return round(value, digits) if finite(value) else None
    return value
