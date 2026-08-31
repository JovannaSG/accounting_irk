# Plan: Restore Καчesне сальдо incl. passive accounts + fix "since" annotation

## Root cause (user's diagnosis — confirmed in code)
- `core/auditor.py:569-581` `check_red_balance` only flags ACTIVE accounts
  (`(Тип == "A") & (net < 0)`) and explicitly ignores passive debit balances
  (comment at lines 572-573). A "red balance" on a passive account (e.g. 68 taxes,
  70 salaries) was therefore NEVER reported — a mismatch vs the UI's promised
  behavior ("или дебет по пассивному счету").
- Strict `net < 0` without epsilon can mis-fire/miss on float rounding
  (100.0 - 100.0 → -1e-11). Use `EPS = 1e-6` (already defined, line 67).

## Additional real bug found (must fix together)
`core/auditor.py:513-527` `_first_occurrence` was changed to group by 3 keys
`["Организация","Счет","Субконто"]` but builds its dict with only **2** keys:
```python
(row[keys[0]], row[keys[1]]): row["Период"]
```
while `_annotate_since:549-550` maps **3-tuples**. Result: the "…отрицательное
сальдо с <дата>" annotation never appears (confirmed: `test_red_balance_period_of_
occurrence` fails — comment is just the base text). Fix: use all 3 keys.

## Code changes — `core/auditor.py`

### 1. Fix `_first_occurrence` (lines 522-525)
Build the dict key with all passed keys (they are consistent across callers):
```python
first = b2.groupby(keys, as_index=False)["Период"].first()
return {
    tuple(row[k] for k in keys): row["Период"]
    for _, row in first.iterrows()
    if row["Период"]
}
```

### 2. Extend `check_red_balance` (lines 568-581)
Use `EPS` and add a passive sub-check, still bypassing AP accounts:
```python
def check_red_balance(self) -> None:
    b = self.balances
    net = b["КонецДебет"] - b["КонецКредит"]  # <0 → кредит > дебет (active red);
                                                # >0 → дебет > кредит (passive red)

    # 1. Активный счет с кредитовым (отрицательным) сальдо
    active = self._annotate_since(
        b, (b["Тип"] == "A") & (net < -EPS),
        "Активный счет имеет кредитовое (отрицательное) сальдо",
        "отрицательное сальдо с",
    )
    if not active.empty:
        self._add("error", "Красное сальдо: активный счет с кредитовым остатком", active)

    # 2. Пассивный счет с дебетовым (отрицательным) сальдо
    passive = self._annotate_since(
        b, (b["Тип"] == "P") & (net > EPS),
        "Пассивный счет имеет дебетовое (отрицательное) сальдо",
        "отрицательное сальдо с",
    )
    if not passive.empty:
        self._add("error", "Красное сальдо: пассивный счет с дебетовым остатком", passive)
```
AP accounts (60/62/71/73/84…) are untouched: `Тип == "AP"` matches neither branch
(they're covered by 4.2 `check_expanded_balance` and 4.5 `check_unclosed_settlements`).

### 3. `RECOMMENDATIONS` (add after line 94, Russian key convention)
```python
"Красное сальдо: пассивный счет с дебетовым остатком": (
    "Проверьте проводки по счету. Дебетовый остаток по пассивному счету "
    "указывает на переплату или ошибку в учете."
),
```

### 4. `_SHORT_PDF_LABELS` (add after line 258)
```python
"Красное сальдо: пассивный счет с дебетовым остатком": "Красное сальдо (P)",
```
(optionally retitle active to "Красное сальдо (A)" for symmetry — keep active as-is
to avoid breaking dashboard tests, which reference the long active title.)

### 5. `SECTION_SPECS` (extend "Красное и развернутое сальдо", line 182-185)
```python
("Красное и развернутое сальдо", (
    "Красное сальдо: активный счет с кредитовым остатком",
    "Красное сальдо: пассивный счет с дебетовым остатком",
    "Развернутое сальдо по аналитике",
)),
```

## Test changes — `tests/test_auditor.py`
- `test_red_balance_active_negative_only` (16-27): account 66 (P, net +100000) is now
  flagged. Update assertion: expect 2 red findings (active 50 + passive 66):
  `set(red[0]["data"]["Счет"]) == {"50"}` and add check that passive "66" is flagged.
  Recommend splitting into explicit active+passive assertions for clarity.
- `test_red_balance_period_of_occurrence` (63-72): should pass again after the
  `_first_occurrence` fix (ensure the "с 28.02.2026" text is restored).
- `test_ap_account_not_red_by_default`, `test_settlement_and_loss_accounts_not_red`:
  unchanged (AP bypass preserved).
- Add a focused new test: passive debit (e.g. 68 P, net>0) → flagged; AP with both
  balances → not red.

## Verification
1. `pytest tests/ -q` green (was: 25 failing before user's comma fix; re-confirm).
2. `ruff check .` clean.
3. App runnable: red balance now appears for passive accounts; active still works;
   AP (60/62/71/73/84) not red; "…сальдо с <дата>" annotation restored.
4. **User action (no code):** the "✖️ Сбросить результаты" button (ui.py:371) resets
   the in-interface history/session so stale cached reports (which hid passive reds)
   are cleared before re-running the audit.

## Files
| File | Change |
|---|---|
| `core/auditor.py` | `_first_occurrence` fix; passive red sub-check + EPS; `RECOMMENDATIONS`; `_SHORT_PDF_LABELS`; `SECTION_SPECS` |
| `tests/test_auditor.py` | update passive-red expectation; add passive/AP coverage |
