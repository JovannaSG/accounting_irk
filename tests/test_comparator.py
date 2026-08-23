import numpy as np
import pandas as pd

from core.comparator import compare_audits

COLUMNS = [
    "Проверка", "Уровень", "Период",
    "Счет", "Субконто", "Сумма", "Комментарий",
]


def _row(check: str, account: str, subconto: str, amount: float = 100.0) -> dict:
    return {
        "Проверка": check,
        "Уровень": "error",
        "Период": "2026-01-31",
        "Счет": account,
        "Субконто": subconto,
        "Сумма": amount,
        "Комментарий": "",
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


# ── 1. Оба пустых ──

def test_both_empty():
    result = compare_audits(pd.DataFrame(), pd.DataFrame())
    for key in ("resolved", "new", "pending"):
        assert key in result
        assert result[key].empty
        assert list(result[key].columns) == COLUMNS


# ── 2. Старый пустой, новый с данными ──

def test_old_empty_new_has_rows():
    new = _df([_row("Красное сальдо", "51", "-")])
    result = compare_audits(pd.DataFrame(), new)
    assert result["resolved"].empty
    assert len(result["new"]) == 1
    assert result["new"].iloc[0]["Счет"] == "51"
    assert result["pending"].empty


# ── 3. Новый пустой, старый с данными ──

def test_new_empty_old_has_rows():
    old = _df([_row("Красное сальдо", "51", "-")])
    result = compare_audits(old, pd.DataFrame())
    assert len(result["resolved"]) == 1
    assert result["resolved"].iloc[0]["Счет"] == "51"
    assert result["new"].empty
    assert result["pending"].empty


# ── 4. Идентичные данные → всё pending ──

def test_identical_data():
    rows = [_row("Красное сальдо", "51", "-"), _row("Развернутое сальдо", "60.01", "ООО Ромашка")]
    data = _df(rows)
    result = compare_audits(data, data.copy())
    assert result["resolved"].empty
    assert result["new"].empty
    assert len(result["pending"]) == 2


# ── 5. Полностью разные данные → split ──

def test_completely_different():
    old = _df([_row("Красное сальдо", "51", "-")])
    new = _df([_row("Незакрытое сальдо", "90.01", "-")])
    result = compare_audits(old, new)
    assert len(result["resolved"]) == 1
    assert result["resolved"].iloc[0]["Счет"] == "51"
    assert len(result["new"]) == 1
    assert result["new"].iloc[0]["Счет"] == "90.01"
    assert result["pending"].empty


# ── 6. Одинаковый ключ, разная сумма → pending с НОВОЙ суммой ──

def test_overlapping_keys_different_amounts():
    old = _df([_row("Красное сальдо", "51", "-", amount=1000.0)])
    new = _df([_row("Красное сальдо", "51", "-", amount=500.0)])
    result = compare_audits(old, new)
    assert result["resolved"].empty
    assert result["new"].empty
    assert len(result["pending"]) == 1
    assert result["pending"].iloc[0]["Сумма"] == 500.0


# ── 7. Ключ = Проверка|Счет|Субконто ──

def test_key_composition():
    a = _df([_row("Красное сальдо", "51", "-")])
    b = _df([_row("Развернутое сальдо", "51", "-")])
    result = compare_audits(a, b)
    assert len(result["resolved"]) == 1
    assert len(result["new"]) == 1
    assert result["pending"].empty


# ── 8. Отсутствие колонки Субконто ──

def test_missing_subconto_column():
    old = pd.DataFrame([{
        "Проверка": "Красное сальдо", "Уровень": "error",
        "Период": "2026-01-31", "Счет": "51", "Сумма": 100.0, "Комментарий": "",
    }])
    new = pd.DataFrame([{
        "Проверка": "Красное сальдо", "Уровень": "error",
        "Период": "2026-01-31", "Счет": "51", "Сумма": 100.0, "Комментарий": "",
    }])
    result = compare_audits(old, new)
    assert len(result["pending"]) == 1


# ── 9. Пробелы в значениях → strip ──

def test_whitespace_stripped():
    old = _df([_row("Красное сальдо", " 51 ", "-")])
    new = _df([_row("Красное сальдо", "51", "-")])
    result = compare_audits(old, new)
    assert len(result["pending"]) == 1
    assert result["resolved"].empty
    assert result["new"].empty


# ── 10. NaN в ключевых колонках →fillna("") ──

def test_nan_in_key_columns():
    old = _df([_row("Красное сальдо", "51", np.nan)])
    new = _df([_row("Красное сальдо", "51", "")])
    result = compare_audits(old, new)
    assert len(result["pending"]) == 1
    assert result["resolved"].empty
    assert result["new"].empty


# ── 11. Исходные данные не мутируются ──

def test_input_not_mutated():
    old = _df([_row("Красное сальдо", "51", "-")])
    new = _df([_row("Красное сальдо", "51", "-")])
    old_cols = list(old.columns)
    new_cols = list(new.columns)
    compare_audits(old, new)
    assert list(old.columns) == old_cols
    assert list(new.columns) == new_cols
    assert "_key" not in old.columns
    assert "_key" not in new.columns


# ── 12. Дублирующиеся строки внутри одного DF ──

def test_duplicate_rows_within_frame():
    row = _row("Красное сальдо", "51", "-")
    old = _df([row, row.copy()])
    new = _df([_row("Развернутое сальдо", "60.01", "Ромашка")])
    result = compare_audits(old, new)
    assert len(result["resolved"]) == 2
    assert result["new"].iloc[0]["Счет"] == "60.01"
    assert result["pending"].empty
