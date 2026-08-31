# Plan: Per-period summary + Remove Субконто from Excel

## Changes

### 1. `core/auditor.py:1161-1174` — rewrite `summary_df()`

Replace the one-row-per-finding-type approach with per-period grouping:

```python
def summary_df(self) -> pd.DataFrame:
    columns = ["Проверка", "Уровень", "Период", "Строк", "Сумма", "Рекомендации"]
    details = self.details_df()
    if details.empty:
        return pd.DataFrame(columns=columns)
    grouped = (
        details
        .groupby(["Проверка", "Уровень", "Период"], sort=False)
        .agg(Строк=("Сумма", "count"), Сумма=("Сумма", "sum"))
        .reset_index()
    )
    grouped["Рекомендации"] = grouped["Проверка"].map(RECOMMENDATIONS).fillna("")
    return grouped[columns]
```

Result: one row per (finding, period) with period-specific amounts instead of one total.

### 2. `core/auditor.py:1503-1506` — drop Субконто/Договор from Excel

After line 1504 (`details = self.details_df()`), add:
```python
details = details.drop(columns=["Субконто", "Договор"], errors="ignore")
```

After line 1506 (`top_findings = self.top_findings_df(...)`), add:
```python
top_findings = top_findings.drop(columns=["Субконто"], errors="ignore")
```

This removes Субконто from "Детальный отчет" and "Обзор" sheets. Dashboard and PDF keep Субконто unchanged.

### 3. No changes to expanded balance (4.2) — correct per ТЗ

## Verification

- `pytest tests/ -q` — all tests pass
- `ruff check .` — clean
