"""Статистика контрактов спреда по шагам до экспирации — по мотивам gas_spreads.py.

Матрица S: строка — контракт (год), столбец d — шагов до склейки окна пары,
0 … width−1. Шаг — день или 4/8/16 часов (analytics.data); day — дней в шаге,
им годовые и удержание переводятся в дни. P — цена, от которой считается
капитал (спот). Нет данных — nan.

Три сетки «шаг входа × шаг выхода» (вход дальше от экспирации, чем выход):
- returns_grid — доход сделки: сторона по знаку среднего изменения,
  % капитала за период, годовые, худший год, доля прибыльных лет, MAE;
- move_grid — движение к нулю: |спред| каждого контракта в своём масштабе,
  + — сузился, − — разошёлся;
- side_grid — куда шёл спред: тот же масштаб, но со знаком, + — вырос (лонг),
  − — упал (шорт).
Плюс zscore_grid и funding_grid (фандинг за удержание, дневной).

numpy: на часовых шагах сетки до 300 × 300 и десятки контрактов; на каждый
шаг входа считается сразу вектор «контракт × все выходы».
"""

import math
import warnings

import numpy as np

NAN = float("nan")


def finite(value):
    return value is not None and value == value and not math.isinf(value)


def _array(rows):
    """Список строк (None — пусто) или массив → float-массив 2D."""
    if isinstance(rows, np.ndarray):
        return rows.astype(float, copy=False)
    if not len(rows):
        return np.zeros((0, 0))
    return np.array([[NAN if value is None else value for value in row] for row in rows], dtype=float).reshape(len(rows), -1)


def _nanmedian(values, axis=0):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(values, axis=axis)


def blank(width):
    return np.full((width, width), NAN)


def _roll(grid, half, axis):
    """Скользящее среднее ±half по оси с пропуском nan; окно без чисел — nan."""
    values = np.where(np.isfinite(grid), grid, 0.0)
    counts = np.isfinite(grid).astype(float)
    pad = [(0, 0), (0, 0)]
    pad[axis] = (1, 0)
    cs_v = np.pad(np.cumsum(values, axis=axis), pad)
    cs_c = np.pad(np.cumsum(counts, axis=axis), pad)
    size = grid.shape[axis]
    index = np.arange(size)
    high = np.minimum(index + half + 1, size)
    low = np.maximum(index - half, 0)
    total = np.take(cs_v, high, axis=axis) - np.take(cs_v, low, axis=axis)
    count = np.take(cs_c, high, axis=axis) - np.take(cs_c, low, axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.where(count > 0, count, 1), NAN)


def smooth2d(grid, half):
    """Скользящее среднее ±half по обеим осям; пустые клетки остаются пустыми."""
    grid = _array(grid)
    if half <= 0:
        return grid.copy()
    out = _roll(_roll(grid, half, 1), half, 0)
    out[~np.isfinite(grid)] = NAN
    return out


def value_at(points, day, tolerance=3):
    """Значение в ближайший к day шаг не дальше tolerance; points — {шаг: значение}."""
    if day in points:
        return points[day]
    for step in range(1, tolerance + 1):
        for candidate in (day - step, day + step):
            if candidate in points:
                return points[candidate]
    return NAN


def matrix(contracts, width, tolerance=3):
    """Контракты с точками (шаг, значение, цена) → S и P размером «контракт × шаг 0 … width−1»."""
    S, P = [], []
    for contract in contracts:
        values = {day: value for day, value, _price in contract["points"]}
        prices = {day: price for day, _value, price in contract["points"]}
        S.append([value_at(values, day, tolerance) for day in range(width)])
        P.append([value_at(prices, day, tolerance) for day in range(width)])
    return np.array(S, dtype=float).reshape(len(S), width), np.array(P, dtype=float).reshape(len(P), width)


# Разброс доходов не меньше 10 % |среднего|: «доход / разброс» не выше 10.
RATIO_SD_FLOOR = 0.1


def returns_grid(S, P, min_hold=10, capital_pct=100.0, cost=0.0, min_years=2, smooth_half=5, metric="ret", day=1.0):
    """Доход сделки «вход за din шагов → выход за dout шагов» по фактическому спреду на выходе.

    Сторона клетки — по знаку суммы изменений: спред падал — продаём (+1),
    рос — покупаем (−1). r = (сторона × (s_вход − s_выход) − cost) / капитал × 100,
    капитал — capital_pct % цены на входе. Годовые — r × 365 / дней удержания
    (шагов × day). MAE — худшее движение против позиции на пути, % капитала,
    максимум по годам.
    """
    S, P = _array(S), _array(P)
    width = S.shape[1] if S.size else 0
    keys = ("ret", "ann", "worst", "win", "sd", "mae", "n", "side")
    G = {key: blank(width) for key in keys}
    for din in range(min_hold, width):
        K = din - min_hold + 1
        # Наибольший и наименьший спред от выхода до входа: копим от din назад.
        span = S[:, :din + 1][:, ::-1]
        run_max = np.fmax.accumulate(np.where(np.isfinite(span), span, -np.inf), axis=1)[:, ::-1][:, :K]
        run_min = np.fmin.accumulate(np.where(np.isfinite(span), span, np.inf), axis=1)[:, ::-1][:, :K]
        s_in = S[:, din][:, None]
        s_out = S[:, :K]
        price = P[:, din][:, None]
        valid = np.isfinite(s_in) & np.isfinite(s_out) & np.isfinite(price) & (price > 0)
        count = valid.sum(axis=0)
        ok = count >= max(min_years, 1)
        if not ok.any():
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            capital = price * capital_pct / 100.0
            change = np.where(valid, s_in - s_out, 0.0)
            side = np.where(change.sum(axis=0) >= 0, 1.0, -1.0)
            hold = (din - np.arange(K)) * day
            result = np.where(valid, (side * change - cost) / capital * 100.0, NAN)
            annual = result * 365.0 / hold
            n = np.where(count > 0, count, 1)
            mean = np.nansum(result, axis=0) / n
            deviation = np.where(valid, (result - mean) ** 2, 0.0).sum(axis=0)
            sd = np.where(count > 1, np.sqrt(deviation / np.where(count > 1, count - 1, 1)), NAN)
            against = np.where(side > 0, run_max - s_in, s_in - run_min)
            adverse = np.where(valid, np.maximum(against, 0.0) / capital * 100.0, -np.inf)
            worst = np.where(valid, annual, np.inf).min(axis=0)
            win = (np.where(valid, result, 0.0) > 0).sum(axis=0) / n
        cells = np.where(ok)[0]
        G["ret"][din, cells] = mean[cells]
        G["ann"][din, cells] = (np.nansum(annual, axis=0) / n)[cells]
        G["worst"][din, cells] = worst[cells]
        G["win"][din, cells] = win[cells]
        G["sd"][din, cells] = sd[cells]
        G["mae"][din, cells] = adverse.max(axis=0)[cells]
        G["n"][din, cells] = count[cells]
        G["side"][din, cells] = side[cells]
    ret, sd = G["ret"], G["sd"]
    with np.errstate(invalid="ignore", divide="ignore"):
        usable = np.isfinite(ret) & np.isfinite(sd) & ((sd > 0) | (ret != 0))
        G["ratio"] = np.where(usable, ret / np.maximum(sd, np.abs(ret) * RATIO_SD_FLOOR), NAN)
    G["smooth"] = smooth2d(G[metric if metric in G else "ret"], smooth_half)
    with np.errstate(invalid="ignore"):
        score = np.where(np.isfinite(G["smooth"]) & np.isfinite(ret) & (ret > 0), G["smooth"], NAN)
    return G, _argmax(score)


def _scales(S, floor_share):
    """Типичный масштаб контракта — медиана |спреда|, не меньше floor_share медианы по контрактам."""
    raw = _nanmedian(np.abs(S), axis=1) if S.size else np.array([])
    finite_raw = raw[np.isfinite(raw)]
    floor = float(np.median(finite_raw)) * floor_share if finite_raw.size else 1.0
    scaled = np.maximum(raw, floor)
    return np.where(np.isfinite(raw) & (scaled > 0), scaled, NAN)


def _normalized_grid(S, change, min_hold, min_years, smooth_half):
    """change(din, K) → (нормированное, абсолютное) «контракт × K выходов», nan — нет данных."""
    width = S.shape[1] if S.size else 0
    M = {key: blank(width) for key in ("move", "agree", "raw", "n")}
    for din in range(min_hold, width):
        K = din - min_hold + 1
        normalized, absolute = change(din, K)
        valid = np.isfinite(normalized)
        count = valid.sum(axis=0)
        cells = np.where(count >= max(min_years, 1))[0]
        if not cells.size:
            continue
        middle = _nanmedian(normalized, axis=0)
        same = (valid & (normalized != 0) & ((normalized > 0) == (middle > 0))).sum(axis=0)
        M["move"][din, cells] = middle[cells]
        M["agree"][din, cells] = (same / np.where(count > 0, count, 1))[cells]
        M["raw"][din, cells] = _nanmedian(np.where(valid, absolute, NAN), axis=0)[cells]
        M["n"][din, cells] = count[cells]
    M["smooth"] = smooth2d(M["move"], smooth_half)
    return M


def move_grid(S, min_hold=10, min_years=2, smooth_half=5, floor_share=0.2):
    """Сужение |спреда| к нулю: |s_вход| − |s_выход| в масштабе контракта, медиана по годам."""
    S = _array(S)
    scales = _scales(S, floor_share)[:, None]

    def change(din, K):
        narrowing = np.abs(S[:, din])[:, None] - np.abs(S[:, :K])
        with np.errstate(invalid="ignore"):
            return narrowing / scales, narrowing

    M = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    with np.errstate(invalid="ignore"):
        score = np.where(np.isfinite(M["smooth"]) & np.isfinite(M["move"]) & (M["move"] > 0), M["smooth"], NAN)
    return M, _argmax(score)


def _agreed(Z):
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(Z["smooth"]) & np.isfinite(Z["agree"]) & (Z["agree"] >= 0.5), np.abs(Z["smooth"]), NAN)


def side_grid(S, min_hold=10, min_years=2, smooth_half=5, floor_share=0.2):
    """Куда шёл спред: s_выход − s_вход в масштабе контракта. + — рос (лонг), − — падал (шорт).

    Лучшая клетка — наибольшее по модулю сглаженное движение, где не меньше
    половины лет согласны со знаком медианы.
    """
    S = _array(S)
    scales = _scales(S, floor_share)[:, None]

    def change(din, K):
        move = S[:, :K] - S[:, din][:, None]
        with np.errstate(invalid="ignore"):
            return move / scales, move

    M = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    return M, _argmax(_agreed(M))


Z_LOOKBACK = 20
Z_MIN_CHANGES = 5


def _local_sigma(row, lookback=Z_LOOKBACK, min_changes=Z_MIN_CHANGES):
    """σ изменений за lookback шагов ДО входа (дальше от экспирации): без заглядывания вперёд.

    sigma[d] — по изменениям row[k] − row[k+1] для k = d … d+lookback−1. Меньше
    min_changes изменений — nan.
    """
    row = np.asarray(row, dtype=float)
    width = row.size
    changes = row[:-1] - row[1:] if width > 1 else np.array([])
    valid = np.isfinite(changes)
    x = np.where(valid, changes, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])
    cn = np.concatenate([[0], np.cumsum(valid)])
    index = np.arange(width)
    low = np.minimum(index, changes.size)
    high = np.minimum(index + lookback, changes.size)
    n = cn[high] - cn[low]
    total = cs[high] - cs[low]
    squares = cs2[high] - cs2[low]
    with np.errstate(invalid="ignore", divide="ignore"):
        variance = np.maximum(squares - total * total / np.where(n > 0, n, 1), 0.0) / np.where(n > 1, n - 1, 1)
    return np.where(n >= max(min_changes, 2), np.sqrt(variance), NAN)


def zscore_grid(S, min_hold=10, min_years=2, smooth_half=5, lookback=Z_LOOKBACK, min_changes=Z_MIN_CHANGES):
    """Локальное движение в сигмах, без привязки к уровню и к нулю.

    z = (s_выход − s_вход) / (σ_вход × √шагов удержания), σ_вход — σ изменений
    контракта за lookback шагов до входа. + — спред вырос (лонг), − — упал (шорт);
    медиана по годам, agree — доля лет с тем же знаком.
    |z| ≈ 1 — обычный шум, 2 и больше — движение заметно сильнее случайного.
    """
    S = _array(S)
    sigmas = np.array([_local_sigma(row, lookback, min_changes) for row in S]).reshape(S.shape)

    def change(din, K):
        move = S[:, :K] - S[:, din][:, None]
        sigma = sigmas[:, din][:, None]
        hold = np.sqrt(din - np.arange(K))
        with np.errstate(invalid="ignore", divide="ignore"):
            normalized = np.where(np.isfinite(sigma) & (sigma > 0), move / (sigma * hold), NAN)
        return normalized, move

    Z = _normalized_grid(S, change, min_hold, min_years, smooth_half)
    return Z, _argmax(_agreed(Z))


def funding_grid(F, P, min_hold=10, capital_pct=100.0, min_years=2):
    """Фандинг за удержание «вход за din дней → выход за dout дней», знак — для лонга спреда.

    F — фандинг на единицу актива за день (контракт × день до склейки), nan — дня нет
    в истории фандинга (вечного ещё не было). Клетка считается по контракту, только если
    весь отрезок покрыт. sum — % капитала за удержание, ann — то же в % годовых,
    rate — средняя дневная ставка к цене в % годовых. Медиана по годам.
    """
    F, P = _array(F), _array(P)
    width = F.shape[1] if F.size else 0
    out = {key: blank(width) for key in ("sum", "ann", "rate", "n")}
    if not width:
        return out
    known = np.isfinite(F)
    cs = np.concatenate([np.zeros((F.shape[0], 1)), np.cumsum(np.where(known, F, 0.0), axis=1)], axis=1)
    cn = np.concatenate([np.zeros((F.shape[0], 1)), np.cumsum(known, axis=1)], axis=1)
    for din in range(max(min_hold, 1), width):
        K = din - min_hold + 1
        outs = np.arange(K)
        hold = din - outs
        # Дни dout … din−1 — их фандинг идёт, пока позиция открыта.
        total = cs[:, din][:, None] - cs[:, :K]
        covered = (cn[:, din][:, None] - cn[:, :K]) == hold
        price = P[:, din][:, None]
        valid = covered & np.isfinite(price) & (price > 0)
        count = valid.sum(axis=0)
        cells = np.where(count >= max(min_years, 1))[0]
        if not cells.size:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(valid, total / (price * capital_pct / 100.0) * 100.0, NAN)
            rate = np.where(valid, total / hold / price * 365.0 * 100.0, NAN)
        out["sum"][din, cells] = _nanmedian(share, axis=0)[cells]
        out["ann"][din, cells] = (_nanmedian(share, axis=0) * 365.0 / hold)[cells]
        out["rate"][din, cells] = _nanmedian(rate, axis=0)[cells]
        out["n"][din, cells] = count[cells]
    return out


def _argmax(grid):
    grid = _array(grid)
    if not grid.size or not np.isfinite(grid).any():
        return None
    flat = int(np.nanargmax(np.where(np.isfinite(grid), grid, -np.inf)))
    return divmod(flat, grid.shape[1])


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
    metric, ret, win, side = (_array(G[key]) for key in ("smooth", "ret", "win", "side"))
    move = _array(D["smooth"])
    z = _array(Z["smooth"]) if Z else np.full(metric.shape, NAN)
    with np.errstate(invalid="ignore"):
        profit = np.isfinite(metric) & np.isfinite(ret) & (ret > 0)
        if not profit.any():
            return {}
        top = metric[profit].max()
        if top <= 0:
            return {}
        width = metric.shape[0]
        lower = np.tril(np.ones((width, width), dtype=bool))  # выход не дальше входа
        direction = -np.sign(side)  # продажа (+1) — шорт
        ok = (profit & lower & (metric >= top * share) & np.isfinite(win) & (win >= min_win)
              & np.isfinite(move) & ((np.abs(move) < d_ignore) | (np.sign(move) == direction))
              & ~(np.isfinite(z) & (np.abs(z) >= z_ignore) & (np.sign(z) != direction)))
    return {(int(din), int(dout)): int(direction[din, dout]) for din, dout in zip(*np.nonzero(ok))}


def zones(G, D, Z, M, share=0.35, min_cells=3, top=10):
    """Связные области годных клеток одной стороны → диапазоны входа и выхода.

    У каждой зоны: сторона, шаги до экспирации покупки и продажи (у лонга покупка —
    вход, у шорта — выход), удержание, лучшая клетка по сглаженной метрике и её
    статистика. Зоны меньше min_cells клеток — шум, отбрасываются.
    """
    cells = valid_cells(G, D, Z, share)
    grids = {key: _array(G[key]) for key in ("smooth", "ret", "ann", "worst", "win", "mae", "ratio", "n")}
    agree = _array(D["agree"])
    z_move = _array(Z["move"]) if Z else None
    m_move = _array(M["move"]) if M else None
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
        din, dout = max(area, key=lambda cell: grids["smooth"][cell])
        entries, exits = [cell[0] for cell in area], [cell[1] for cell in area]
        holds = [a - b for a, b in area]
        entry, exit_ = [min(entries), max(entries)], [min(exits), max(exits)]
        long = direction > 0
        value = (lambda grid: float(grid[din, dout]))
        found.append({
            "side": "лонг" if long else "шорт",
            "buy": entry if long else exit_, "sell": exit_ if long else entry,
            "entry": entry, "exit": exit_, "hold": [min(holds), max(holds)], "cells": len(area),
            "best": [din, dout], "metric": value(grids["smooth"]),
            "ret": value(grids["ret"]), "ann": value(grids["ann"]), "worst": value(grids["worst"]),
            "win": value(grids["win"]), "mae": value(grids["mae"]), "ratio": value(grids["ratio"]),
            "n": value(grids["n"]), "agree": value(agree),
            "z": value(z_move) if z_move is not None else NAN,
            "narrowing": value(m_move) if m_move is not None else NAN,
        })
    found.sort(key=lambda zone: zone["metric"], reverse=True)
    return found[:top]


def clean(value, digits=5):
    """nan и бесконечности → None для JSON, числа — с ограниченной точностью."""
    if isinstance(value, np.ndarray):
        rounded = np.round(value.astype(float), digits)
        out = rounded.astype(object)
        out[~np.isfinite(rounded)] = None
        return out.tolist()
    if isinstance(value, list):
        return [clean(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: clean(item, digits) for key, item in value.items()}
    if isinstance(value, tuple):
        return [clean(item, digits) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return round(value, digits) if finite(value) else None
    return value
