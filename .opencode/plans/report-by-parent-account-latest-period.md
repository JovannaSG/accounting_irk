# Plan: Report errors by parent account (60 not 60.01) + only latest period

## Problem
1. Findings are recorded per sub-account ("60.01", "60.02"…), bloating the reports
   (dashboard, PDF, Excel "По счетам"/"Детальный отчет"/"Обзор").
2. In whole-period mode a recurring error (e.g. a negative balance held for a year)
   adds one row per month → `details`/`amount`/sums inflate (12 × −100 = −1200).

## Approach (user-confirmed)
Single choke point `_add()` (`core/auditor.py:503-517`) — every finding flows through
it (verified: only `_add` appends to `self.errors`, lines 589–1144). In the interceptor:
- Collapse `Счет` to **parent account** (`account_group`, already at line 302: `60.01→60`).
- Keep only the **most recent `Период`** per (parent account + субконто) group (drop earlier months).
- Сумма = **sum of |net| magnitudes** across sub-accounts (never cancels opposite balances).
- **Only OSV/balance findings**: those whose `data` has `КонецДебет`/`КонецКредит`.
  ML/NLP (amount anomaly, turnover jump, dupes, 115-ФЗ) are left untouched (confirmed).

## Where to put the transform in `_add`
Insert a private helper `_collapse_saldo_data(data) -> pd.DataFrame` at the top of `_add`.
Apply it before the `amount` computation (lines 507-516) so both `data` and `amount`
reflect the collapsed view. Guard every step defensively.

### Helper semantics (`_collapse_saldo_data`)
1. If `data.empty` or lacks `Счет` OR lacks **both** `КонецДебет`+`КонецКредит`
   (= non-balance finding) → return unchanged.
2. Otherwise require `Период`; if absent, still collapse accounts but skip the
   latest-period filter.
3. Build a sort key for `Период` (date-aware, e.g. `pd.to_datetime(..., format="mixed")`
   like `_first_occurrence:526`); sort rows by (parent_account, Субконто, period) desc.
4. Keep the latest-period row per (parent_account, Субконто): reset `Счет` to parent,
   retain that row's `Комментарий` (holds the "…с <дата>" annotation from `_annotate_since`),
   `Период`, `Субконто`, `Организация`, `Договор`.
5. Сумма of the kept row = sum of `|КонецДебет − КонецКредит|` over all its sub-account
   source rows (only those flagged). Keep `КонецДебет`/`КонецКредит` of the latest row
   as-is (they're informational); the authoritative shown сумма is the aggregated one.
6. If "Развернутое сальдо" finding (both sides in one row): preserve existing behaviour
   (expanded balance is already one row per аналитика; just map Счет→parent).

### `amount` (recomputed from collapsed frame)
Keep the existing branches at 507-516 but run them on the **collapsed** `data`:
- non-развернутое: `(КонецДебет − КонецКредит).abs().sum()`
- развернутое: `.sum().sum()`
This automatically kills the 12-month inflation.

## Consumers affected (all read from `self.errors`, so collapsed once, correct everywhere)
- `details_df` / `summary_df` / `top_findings_df` (`auditor.py:1212-1248`)
- Excel "По счетам"/"Детальный отчет"/"Обзор" (`auditor.py:1526+`, uses `accounts_summary_df` at 1472, `accounts_with_errors` at 1380 — both derive from `details_df` → now parent accounts)
- PDF sections (`auditor.py:1250-1316`, `_project_saldo` at 1342)
- Dashboard master/detail (`core/dashboard.py:103-144, 76-92`; `app/ui.py:479-517`) — will now show parent accounts.

## Tests (`tests/test_auditor.py`, `tests/test_db.py`, `tests/test_dashboard.py`)
- Update the red-balance tests to expect **parent** accounts where sub-accounts were
  expected (e.g. `test_ap_account_not_red_by_default` uses "60.01"→ expect "60").
- `test_settlement_osv_account_breakdown_and_split_warning` expects "60.01" & "60.02"
  in the cell → should become "60" after collapse.
- Add new tests:
  - repeated monthly red balance (`-100` each of 12 months) → **1 row**, `amount == 100` (not 1200), latest period.
  - sub-accounts 60.01 (+45000) and 60.02 (−30000) same субконто → collapsed "60",
    sum = 75000 (sum of |net|, no cancellation).
  - ML/NLP finding untouched (still every event).
- Keep `test_red_balance_period_of_occurrence`'s "с 28.02.2026" annotation preserved
  after collapse.

## Files
| File | Change |
|---|---|
| `core/auditor.py` | add `_collapse_saldo_data`; call in `_add` before `amount` |
| `tests/test_auditor.py` | update account expectations; add collapse/latest-period tests |
| `tests/test_dashboard.py` | if any master/detail test asserts sub-account strings |

## Open checks during implementation
- Whole-period mode vs "по месяцам" mode (ui.py:1120-1137): month-mode already stores
  one period per history entry; intra-entry collapse still applies (harmless there).
  Cross-entry dashboard summation is separate — confirm desired once collapse lands.
- Preserve `Комментарий` "since <date>" across collapse.
