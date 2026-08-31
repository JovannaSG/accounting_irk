"""
Проверки целостности загруженных данных ОСВ.

Находки здесь носят исключительно предупредительный характер (advisory):
они никогда не блокируют аудит и не смешиваются с учётными ошибками.
Эвристики вероятностны — задача показать бухгалтеру жёлтые предупреждения
ДО просмотра результатов аудита, чтобы отличить артефакт загрузки данных
от реального нарушения (например, фантомное «красное сальдо» 58.03,
возникшее из-за сдвига колонок, а не из-за фактического остатка).
"""

from typing import Any

import pandas as pd

# Русские тексты предупреждений для UI
WARN_COLUMN_SHIFT = (
    "Аномалия математического баланса: Остаток на конец не сходится с оборотами. "
    "Вероятно, при загрузке колонки сместились (например, «Обороты» загрузились как "
    "«Остаток на конец»)."
)
WARN_MAGNITUDE_MISMATCH = (
    "Аномалия сумм: Средние обороты многократно превышают остатки. "
    "Убедитесь, что колонки Оборотов и Остатков не перепутаны местами в исходном файле."
)


def check_bookkeeping_identity(df: pd.DataFrame) -> list[str]:
    """Проверка 1: математическое тождество баланса.

    (КонецДебет - КонецКредит) - (НачалоДебет - НачалоКредит) - (ОборотДебет - ОборотКредит) == 0
    """
    warnings: list[str] = []
    required_cols = [
        "НачалоДебет", "НачалоКредит",
        "ОборотДебет", "ОборотКредит",
        "КонецДебет", "КонецКредит",
    ]

    if not all(c in df.columns for c in required_cols) or df.empty:
        return warnings

    beg_net = df["НачалоДебет"].fillna(0) - df["НачалоКредит"].fillna(0)
    end_net = df["КонецДебет"].fillna(0) - df["КонецКредит"].fillna(0)
    turn_net = df["ОборотДебет"].fillna(0) - df["ОборотКредит"].fillna(0)

    # Уравнение: Конец = Начало + Обороты  =>  Конец - Начало - Обороты = 0
    diff = (end_net - beg_net - turn_net).abs()

    # Если более 10% строк с ненулевыми остатками нарушают базовое уравнение
    # больше чем на 1 рубль (ошибка округления) — колонки, скорее всего, сдвинуты.
    active_rows = df[(df[required_cols] > 0).any(axis=1)]
    if not active_rows.empty:
        violation_rate = (diff > 1.0).sum() / len(active_rows)
        if violation_rate > 0.1:  # порог 10%
            warnings.append(WARN_COLUMN_SHIFT)

    return warnings


def check_magnitude_correlation(df: pd.DataFrame) -> list[str]:
    """Проверка 2: соотношение оборотов и остатков по модулю.

    Ловит случай, когда «Обороты» ошибочно попали в колонку «Остаток».
    """
    warnings: list[str] = []
    if "ОборотДебет" not in df.columns or "КонецДебет" not in df.columns or df.empty:
        return warnings

    mean_turnover = df["ОборотДебет"].fillna(0).mean()
    max_balance = df["КонецДебет"].fillna(0).max()

    # Средний оборот аномально больше максимального остатка на конец —
    # при этом уравнение баланса не сходится. Огромный красный флаг.
    if max_balance > 0 and mean_turnover > (max_balance * 10):
        warnings.append(WARN_MAGNITUDE_MISMATCH)

    return warnings


def validate_osv_integrity(df: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    """Агрегирует все проверки целостности.

    Возвращает словарь, который можно безопасно вложить в `info`, возвращаемый
    загрузчиками.
    """
    findings: list[str] = []
    findings.extend(check_bookkeeping_identity(df))
    findings.extend(check_magnitude_correlation(df))

    provenance = {
        "db_name": meta.get("db_name", "Неизвестная база"),
        "source_type": meta.get("source_type", "file"),
        "period_parsed": meta.get("period", "Не определен"),
        "org_parsed": meta.get("organization", "Не определена"),
    }

    return {
        "integrity_warnings": list(set(findings)),  # убираем дубли
        "provenance": provenance,
        "is_suspicious": len(findings) > 0,
    }
