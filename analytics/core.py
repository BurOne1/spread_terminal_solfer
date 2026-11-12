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


# Разброс доходов не меньше 10 % |среднего|: «доход / разброс» не выше 10.
RATIO_SD_FLOOR = 0.1


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
        [ret / max(sd, abs(ret) * RATIO_SD_FLOOR) if finite(ret) and finite(sd) and (sd > 0 or ret != 0) else NAN
         for ret, sd in zip(row_ret, row_sd)]
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


Z_LOOKBACK = 20
Z_MIN_CHANGES = 5


def _local_sigma(row, lookback=Z_LOOKBACK):
    """σ дневных изменений за lookback дней ДО входа (дальше от экспирации): без заглядывания вперёд.

    sigma[d] — по изменениям row[k] − row[k+1] для k = d … d+lookback−1. Меньше
    Z_MIN_CHANGES изменений — nan.
    """
    width = len(row)
    changes = [row[k] - row[k + 1] if finite(row[k]) and finite(row[k + 1]) else NAN for k in range(width - 1)]
    sigma = []
    for day in range(width):
        window = [value for value in changes[day:day + lookback] if finite(value)]
        sigma.append(stdev(window) if len(window) >= Z_MIN_CHANGES else NAN)
    return sigma


def zscore_grid(S, min_hold=10, min_years=2, smooth_half=5, lookback=Z_LOOKBACK):
    """Локальное движение в сигмах, без привязки к уровню и к нулю.

    z = (s_выход − s_вход) / (σ_вход × √дней удержания), σ_вход — σ дневных
    изменений контракта за lookback дней до входа. + — спред вырос (лонг),
    − — упал (шорт); медиана по годам, agree — доля лет с тем же знаком.
    |z| ≈ 1 — обычный шум, 2 и больше — движение заметно сильнее случайного.
    """
    sigmas = [_local_sigma(row, lookback) for row in S]

    def change(din, dout):
        hold = math.sqrt(din - dout)
        for row, sigma in zip(S, sigmas):
            if finite(row[din]) and finite(row[dout]) and finite(sigma[din]) and sigma[din] > 0:
                move = row[dout] - row[din]
                yield move / (sigma[din] * hold), move

    Z = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    score = [[abs(value) if finite(value) and finite(Z["agree"][din][dout]) and Z["agree"][din][dout] >= 0.5 else NAN
              for dout, value in enumerate(row)] for din, row in enumerate(Z["smooth"])]
    return Z, _argmax(score)


def _argmax(grid):
    best, where = -math.inf, None
    for din, row in enumerate(grid):
        for dout, value in enumerate(row):
            if finite(value) and value > best:
                best, where = value, (din, dout)
    return where


def _sign(value):
    return (value > 0) - (value < 0)


def valid_cells(G, D, Z, share=0.35, min_win=0.4, d_ignore=0.1, z_ignore=0.5):
    """Клетки зон: основа — сетка дохода, остальные сетки только проверяют.

    - доход: сглаженная метрика ≥ share × лучшей, средний доход > 0, в плюс ≥ min_win;
    - направление: «куда шёл спред» не против стороны (лонг — рос, шорт — падал).
      Важен знак; слабое движение |D| < d_ignore масштаба контракта не мешает;
    - z: если |z| ≥ z_ignore, знак не должен противоречить стороне.
    Вернуть {(вход, выход): +1 лонг / −1 шорт}.
    """
    width = len(G["smooth"])
    profit = [G["smooth"][din][dout] for din in range(width) for dout in range(width)
              if finite(G["smooth"][din][dout]) and finite(G["ret"][din][dout]) and G["ret"][din][dout] > 0]
    top = max(profit, default=NAN)
    if not finite(top) or top <= 0:
        return {}
    cells = {}
    for din in range(width):
        for dout in range(din + 1):
            metric, ret, win, side = (G[key][din][dout] for key in ("smooth", "ret", "win", "side"))
            if not (finite(metric) and finite(ret) and ret > 0 and metric >= top * share and finite(win) and win >= min_win):
                continue
            direction = -_sign(side)  # продажа (+1) — шорт
            move = D["smooth"][din][dout]
            if not finite(move) or (abs(move) >= d_ignore and _sign(move) != direction):
                continue
            z = Z["smooth"][din][dout] if Z else NAN
            if finite(z) and abs(z) >= z_ignore and _sign(z) != direction:
                continue
            cells[(din, dout)] = direction
    return cells


def zones(G, D, Z, M, share=0.35, min_cells=3, top=10):
    """Связные области годных клеток одной стороны → диапазоны входа и выхода.

    У каждой зоны: сторона, дни до экспирации покупки и продажи (у лонга покупка —
    вход, у шорта — выход), удержание, лучшая клетка по сглаженной метрике и её
    статистика. Зоны меньше min_cells клеток — шум, отбрасываются.
    """
    cells = valid_cells(G, D, Z, share)
    seen, found = set(), []
    for start in sorted(cells):
        if start in seen:
            continue
        direction, stack, area = cells[start], [start], []
        seen.add(start)
        while stack:
            din, dout = stack.pop()
            area.append((din, dout))
            # Соседи и по диагонали: зона не рвётся на ступеньках границы сетки.
            for step in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                near = (din + step[0], dout + step[1])
                if near not in seen and cells.get(near) == direction:
                    seen.add(near)
                    stack.append(near)
        if len(area) < min_cells:
            continue
        din, dout = max(area, key=lambda cell: G["smooth"][cell[0]][cell[1]])
        entries, exits = [cell[0] for cell in area], [cell[1] for cell in area]
        holds = [a - b for a, b in area]
        entry, exit_ = [min(entries), max(entries)], [min(exits), max(exits)]
        long = direction > 0
        found.append({
            "side": "лонг" if long else "шорт",
            "buy": entry if long else exit_, "sell": exit_ if long else entry,
            "entry": entry, "exit": exit_, "hold": [min(holds), max(holds)], "cells": len(area),
            "best": [din, dout], "metric": G["smooth"][din][dout],
            "ret": G["ret"][din][dout], "ann": G["ann"][din][dout], "worst": G["worst"][din][dout],
            "win": G["win"][din][dout], "mae": G["mae"][din][dout], "ratio": G["ratio"][din][dout],
            "n": G["n"][din][dout], "agree": D["agree"][din][dout],
            "z": Z["move"][din][dout] if Z else NAN, "narrowing": M["move"][din][dout] if M else NAN,
        })
    found.sort(key=lambda zone: zone["metric"], reverse=True)
    return found[:top]


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
