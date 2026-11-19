"""Вейвлет-денойзинг ряда: Добеши 4, мягкий универсальный порог (VisuShrink).

Ряд раскладывается до максимального уровня, шум σ оценивается по самому
мелкому уровню деталей (MAD / 0.6745), детали всех уровней сжимаются мягким
порогом λ = σ·√(2 ln n), ряд собирается обратно той же длины. Края
продолжаются зеркально (mode="symmetric"): у спреда начало и конец окна
разные, периодическое продолжение дало бы скачок на стыке.

Сглаживание видит весь ряд, в том числе будущее относительно точки: для
статистики по прошлым контрактам это сглаживание, а не сигнал в реальном времени.
"""

import math

import numpy as np
import pywt

WAVELET = "db4"
# Короче — разложение почти пустое, ряд не трогаем.
MIN_POINTS = 32


def wavelet_denoise(values, wavelet=WAVELET):
    """Сгладить ряд чисел (без пропусков). Вернуть список той же длины."""
    data = np.asarray(values, dtype=float)
    n = data.size
    if n < MIN_POINTS or not np.isfinite(data).all():
        return list(map(float, data))
    level = pywt.dwt_max_level(n, pywt.Wavelet(wavelet).dec_len)
    if level < 1:
        return list(map(float, data))
    coeffs = pywt.wavedec(data, wavelet, mode="symmetric", level=level)
    finest = coeffs[-1]
    sigma = float(np.median(np.abs(finest))) / 0.6745
    if sigma <= 0:
        return list(map(float, data))
    threshold = sigma * math.sqrt(2.0 * math.log(n))
    coeffs[1:] = [pywt.threshold(detail, threshold, mode="soft") for detail in coeffs[1:]]
    return list(map(float, pywt.waverec(coeffs, wavelet, mode="symmetric")[:n]))
