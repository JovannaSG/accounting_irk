# Plan: Apply user's rewritten core/auditor.py (parent accounts + latest period)

## What the user's code does (verified, already includes prior fixes)
- `_add()` interceptor: collapses `Счет` → parent (`_group_account_string`, e.g. "60.01, 62.02"→"60, 62"),
  for balance checks groups by (Период, Организация, Счет, Субконто, Договор) summing
  КонецДебет/КонецКредит/Сумма, then `drop_duplicates(keep="last")` → only latest period.
- `details_df`/`summary_df`/`top_findings_df`/`accounts_summary_df`/`report` read from collapsed
  data → dashboard, PDF, Excel all show parent accounts and latest-period amounts.
- Keeps passive red-balance branch, all-keys `_first_occurrence`, restored `advance_vs_debt` `_add`.

## Two agreed refinements to the user's file
1. **Chronological latest-period sort.** Replace `data.sort_values("Период")` with a
   date-aware key before the `keep="last"` dedup, e.g. compute
   `data["_pkey"] = period_sort_series(data["Период"])`, sort by it, drop duplicates,
   then drop `_pkey`. (Fixes 31.12.2025 vs 01.01.2026 / dayfirst — same helper `_first_occurrence`
   uses.)
2. **Split ML/NLP scope for Счет collapse.** Gate the `Тип=="A"/"P"/AP` parent-account collapse
   to the same `balance_checks` set; ML/NLP findings keep their raw sub-account values.
   Implementation: in `_add`, only run `data["Счет"].apply(_group_account_string)` when the
   finding is a balance check (i.e. after `if title in balance_checks:`), or equivalently guard
   by the presence of balance columns. Confirm ML column set: ML amount-anomaly and turnover-jump
   both carry `Счет` and must be excluded; NLP has no `Счет`.

## Apply steps
1. Overwrite `core/auditor.py` with the user's version (verbatim) then apply refinements 1 & 2.
2. `ruff check` — fix any style issues (their file is mostly clean).
3. `pytest tests/ -q` — repair tests that assert sub-account strings now collapsed to parent:
   - `tests/test_auditor.py::test_accounts_with_errors_and_account_report_df` (696-698: "60.01"→"60")
   - `tests/test_auditor.py::test_account_report_df_multiple_accounts_in_cell` (705+: "62.01"→"62")
   - `tests/test_auditor.py::test_accounts_summary_df_structure` (753+): account = "60" not "60.01";
     `Кол-во нарушений` sum semantics may change (now per parent). Reconcile to new collapsed rows.
   - Any other test asserting `details["Счет"]` sub-accounts or long recommendation strings / PDF
     short labels ("Красное сальдо (А)"/"(П)" and shortened RECOMMENDATIONS) — audit dashboard,
     ml, comparator, account_pass tests for such assertions and update.
   - Add new tests: 12-month recurring red balance → 1 row, amount = one period (not ×12);
     sub-accounts 60.01(+45000)+60.02(−30000) same субконто → "60", Сумма = 75000 (no cancel);
     ML turnover-jump keeps raw `Счет` (not collapsed); chronological latest period for
     dayfirst format (31.01.2026 vs 01.02.2026).
4. Confirm `report()`/`accounts_summary_df` totals no longer inflate across months.

## Files
| File | Action |
|---|---|
| `core/auditor.py` | replace with user's version + 2 refinements |
| `tests/test_auditor.py` | update sub-account expectations; add collapse/latest-period/ML-scope tests |
| `tests/test_dashboard.py`, `tests/test_ml.py`, `tests/test_db.py`, `tests/test_account_pass.py`, `tests/test_comparator.py` | update any assertions on sub-account `Счет`, recommendation strings, short labels |

## Verification
- `pytest tests/ -q` green; `ruff check .` clean.
- Manual: whole-period base with a year-long −100 red balance reports 100 (not 1200) on parent
  account; sub-account checks still run (flagged then collapsed for display).
