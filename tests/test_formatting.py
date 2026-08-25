import pandas as pd

from core.formatting import period_sort_series


def _ordered(values: list[str]) -> list[str]:
    df = pd.DataFrame({"Период": values})
    df["_key"] = period_sort_series(df["Период"])
    return df.sort_values(
        "_key", kind="stable", na_position="last"
    )["Период"].tolist()


# ── 1. Смешанные форматы периодов упорядочиваются по календарю ──

def test_mixed_formats_chronological():
    values = _ordered(["01.02.2026", "2026-01-31", "31.12.2025"])
    assert values == ["31.12.2025", "2026-01-31", "01.02.2026"]


# ── 2. Нераспознанные значения — в конце, стабильным порядком ──

def test_unparseable_go_last_stable():
    values = _ordered(["Декабрь", "31.12.2025", "Январь?", "01.02.2026"])
    assert values == ["31.12.2025", "01.02.2026", "Декабрь", "Январь?"]


# ── 3. Российский формат читается как дд.мм (день ≤ 12 не путается) ──

def test_dayfirst_parsing():
    values = _ordered(["05.03.2026", "06.02.2026", "07.01.2026"])
    assert values == ["07.01.2026", "06.02.2026", "05.03.2026"]
