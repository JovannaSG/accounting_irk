"""
Форматирование чисел и дат в российских соглашениях для экспорта
(PDF/Excel) и текстов комментариев к находкам.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def fmt_rub(value: Any) -> str:
    """
    Денежная сумма: 112 000 000,00 (пробел — разделитель тысяч).
    Нечисловые значения возвращаются как есть.
    """

    try:
        f = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    return f"{f:,.2f}".replace(",", " ").replace(".", ",")


def fmt_num(value: Any) -> str:
    """
    Коэффициент/процент: целые без дробной части, иначе один знак (3,5).
    """

    try:
        f = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:.1f}".replace(".", ",")


def fmt_date(value: Any) -> str:
    """
    Дата в формате дд.мм.гггг; строки и пустые значения не трогаем.
    """

    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    text = str(value).strip()
    # ISO-строки «2026-01-31 …» → «31.01.2026»
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            year, month, day = int(text[:4]), int(text[5:7]), int(text[8:10])
        except ValueError:
            return text
        return f"{day:02d}.{month:02d}.{year:04d}"
    return text


def period_sort_series(values: Any) -> pd.Series:
    """
    Ключ хронологической сортировки периодов.

    Строки «Период» бывают разными («2026-01-31», «31.01.2026», «Январь»),
    поэтому лексикографическая сортировка ломает порядок месяцев (особенно
    на границе годов). Здесь даты распознаются; нераспознанные значения
    идут в конце, сохраняя исходный относительный порядок.
    """

    parsed = pd.to_datetime(
        pd.Series(values, dtype=object),
        errors="coerce",
        format="mixed",
        # Российские даты «дд.мм.гггг»: дни ≤ 12 иначе читались бы как месяцы
        dayfirst=True,
    )
    return parsed
