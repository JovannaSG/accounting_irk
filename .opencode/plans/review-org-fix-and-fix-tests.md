# Plan: Review of user's org fix + fix 25 failing tests

## Review result — user's fix is good for the reported bug
`core/api_client.py` now:
- Removes hardcoded `Organization_Key` from global `_OSV_SELECT`.
- `fetch_osv` / `fetch_osv_account_subconto` take `with_org: bool = True` and, on 400/404, fall back to a request without `Организация_Key`. → Solves the 400 → silent-empty → "Выбранные фильтры не содержат данных" bug. ✓
- `_record_organization` handles flat GUID + nested dict. ✓

## Problem 1 (causes 24 of 25 test failures): missing comma in DETAIL_COLUMNS
File: `core/auditor.py:161-166`
```python
DETAIL_COLUMNS: list[str] = [
    "Проверка", "Уровень", "Период", "Организация"   # ← MISSING COMMA
    "Счет", "Субконто", "Договор",
    "Дебет", "Кредит",
    "Сумма", "Комментарий"
]
```
Python string-concatenates `"Организация" "Счет"` → single column `ОрганизацияСчет`. Every `details_df()`-based consumer (`accounts_with_errors`, `accounts_summary_df`, Excel/PDF export, dashboard) then raises `KeyError: 'Счет'`.

**Fix:** add the comma:
```python
    "Проверка", "Уровень", "Период", "Организация",
    "Счет", "Субконто", "Договор",
    ...
```

## Problem 2: `fetch_osv_monthly` still swallows real errors (robustness, not the 400-bug)
`core/api_client.py:394` — `except Exception: agg_dfs_by_month[...] = empty` still converts genuine failures (connection, 500, bad period) into an empty base → misleading filter message. Proposal: log + surface only accumulate a helpful summary; at minimum re-raise fatal errors after the org-fallback already ran. (Optional; the 400-org case is already handled by the `with_org` fallback.)

## Problem 3: tests need updating for the new `Организация` column
`tests/test_loaders.py::test_load_real_xls_basics` asserts the exact OSV column list without `Организация`. Update the expected columns to include `"Организация"` after `"Тип"`.

## Verification
1. Apply the comma fix → re-run tests; expect the 24 KeyError failures to pass.
2. Update the one column-order assertion in `test_loaders.py`.
3. `ruff check .` clean.
4. Manual: load base without org support → data loads; with org support → per-org datasets.

## Files
| File | Change |
|---|---|
| `core/auditor.py:162` | add missing comma in `DETAIL_COLUMNS` |
| `tests/test_loaders.py:68` | add `"Организация"` to expected columns |
| `core/api_client.py` (optional) | improve silent-swallow in `fetch_osv_monthly` |
