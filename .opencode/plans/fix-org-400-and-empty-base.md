# Plan: Fix "Выбранные фильтры не содержат данных" on base load + proper Organization support

## Root cause (confirmed)
1. `core/api_client.py:34` hardcodes `"Organization_Key"` into `_OSV_SELECT`. On the user's 1C config this causes HTTP 400.
2. `fetch_osv_monthly` `except Exception:` (line ~376) silently swallows the error and returns an **empty DataFrame**.
3. Empty `balances` → no periods → `selected_periods = []` → the period guard `balances[balances["Период"].isin(selected_periods)].empty` (ui.py:1055) is true → **"Выбранные фильтры не содержат данных" on first load**.

Additional facts from 1C OData docs:
- `$select` on virtual tables (`BalanceAndTurnovers`) is **ignored** — platform returns all ~88 fields.
- The correct organization dimension in standard configs is the Russian resource **`Организация`** (select `Организация/Description`, `$expand=Организация`), not `Organization_Key`.

## Goal
1. Restore reliable base loading (no single field can empty the whole base).
2. Implement organization support properly and tolerantly.
3. Surface real API errors instead of a misleading filter message.

---

## Changes

### 1. `core/api_client.py` — remove fragile `Organization_Key`, make org detection tolerant

**a) Revert `_OSV_SELECT` / subconto `base_select`** to NOT include `Organization_Key`:
```python
_OSV_SELECT: str = ",".join(
    ["Account_Key"]
    + [candidates[0] for candidates in _OSV_FIELDS.values()]
)
```
and subconto `base_select = ["Account_Key", "ExtDimension1", "ExtDimension2", ...]`.

**b) Attempt organization via `$expand` tolerantly** in `fetch_osv`:
- Build params with `"$expand": "Организация"` (and select `Организация/Description`).
- On success and if records carry an `Организация` object with a `Description`/`Ref_Key`, populate the `Организация` column in `_records_to_osv`.
- On 400/ValueError → **fall back** to retrying without `$expand`, set an instance flag `self._org_supported = False`, log a warning. Never let org requests empty the base.

**c) `_record_organization(rec)`** — handle nested `Организация` object (Description/Наименование/Ref_Key) in addition to a flat GUID:
```python
def _record_organization(self, rec):
    val = rec.get("Организация") or rec.get("Organization")
    if isinstance(val, dict):
        return val.get("Description") or val.get("Наименование") or val.get("Name") or ""
    if val:
        return self._guid_to_name.get(str(val), str(val))
    return ""
```

**d) Stop silently swallowing real errors in `fetch_osv_monthly`**:
- Keep per-month tolerance but only for expected empty months (no records). If a month fetch raises `ValueError` (connection/auth/400), **do not convert to an empty frame** — propagate so the UI shows the real error via the existing `st.error`/`st.sidebar.error` handlers. This ensures a genuinely broken base is reported instead of a silent empty.

**e) `extract_organizations(df)`** — keep, but it now works only when the `Организация` column is populated (which happens only when the config supports it).

### 2. `app/ui.py` — clearer guard message + per-dataset org (already added)

- Improve the guard (line 1055) so when `balances` is genuinely empty it explains the real cause (e.g. "1С вернул пустые данные — проверьте доступ/период") rather than "Выбранные фильтры не содержат данных".
- Multi-org split in single-base and batch modes **already implemented** and works only when the `Организация` column is present; otherwise falls back to single-org mode. No change needed beyond relying on Fix 1.

### 3. Keep
- `auditor.py` `OSV_COLUMNS` keeps `"Организация"` (harmless, ignored when absent).
- `ui.py` period filter no longer has the fake `""` option (already done), so the guard only fires on genuinely empty data.

---

## Behavior after fix
- **Configs without org support:** base loads normally (single-org), no 400, no empty swallow. Bug fixed.
- **Configs with org support:** `$expand=Организация` populates the column; multi-org bases split into per-org datasets and audits run per org.
- **Broken/unauthorized base:** real API error shown, not a silent empty + misleading filter message.

## Files
| File | Change |
|---|---|
| `core/api_client.py` | remove Organization_Key from selects; tolerant `$expand` org detection with fallback; `_record_organization` handles nested object; stop silent swallow in `fetch_osv_monthly` |
| `app/ui.py` | clearer empty-data guard message |

## Verification
- `pytest tests/ -q` — all pass (update `test_api_client` if it asserts `Organization_Key` in select).
- `ruff check .` — clean.
- Manual: load a base without org support → data loads, no filter error. Load multi-org base → per-org datasets.
