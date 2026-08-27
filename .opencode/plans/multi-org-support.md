# Plan: Multi-Organization Support

## Problem
When a single 1C:Fresh base contains multiple organizations, the app mixes all orgs' data together. No organization field in data, no filtering, no separate auditing.

## Solution
Add `Organization_Key` to API select, resolve GUID→name via existing `_guid_to_name` cache, split multi-org data into separate datasets for independent auditing.

---

## Changes

### 1. `core/api_client.py`

**a) Add `Organization_Key` to `_OSV_SELECT` (line 33):**
```python
_OSV_SELECT: str = ",".join(
    ["Account_Key", "Organization_Key"]
    + [candidates[0] for candidates in _OSV_FIELDS.values()]
)
```

**b) Add `Catalog_Организации` to `_prefetch_catalogs()` (line 100):**
```python
catalogs = [
    "Catalog_Контрагенты",
    "Catalog_ДоговорыКонтрагентов",
    "Catalog_ФизическиеЛица",
    "Catalog_Организации",
]
```

**c) Add `_record_organization()` method (after `_record_contract` at line 195):**
```python
def _record_organization(self, rec: dict) -> str:
    val = rec.get("Organization_Key") or rec.get("Организация_Key")
    if not val:
        return ""
    if isinstance(val, dict):
        return val.get("Description") or val.get("Наименование") or str(val)
    val_str = str(val)
    return self._guid_to_name.get(val_str, val_str)
```

**d) Populate "Организация" in `_records_to_osv()` (line 215-228):**
Add to each row dict:
```python
"Организация": self._record_organization(rec),
```

**e) Add `extract_organizations()` static method:**
```python
@staticmethod
def extract_organizations(df: pd.DataFrame) -> list[str]:
    if "Организация" not in df.columns:
        return []
    return sorted(df["Организация"].dropna().unique().tolist())
```

### 2. `core/auditor.py`

**Add "Организация" to `OSV_COLUMNS` (line 71):**
```python
OSV_COLUMNS: list[str] = [
    "Период", "Счет", "Субконто", "Тип", "Организация",
    "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
    "КонецДебет", "КонецКредит",
]
```

Extra column ignored by all existing checks — no auditor changes needed.

### 3. `app/ui.py`

**a) Single-base mode (☁️) — after fetch (lines 724-732):**
After fetching, extract unique orgs. If >1 org, split into multiple datasets:
```python
orgs = OneCClient.extract_organizations(api_balances)
if len(orgs) > 1:
    for org in orgs:
        org_df = api_balances[api_balances["Организация"] == org].copy()
        datasets_to_process.append({
            "name": f"{api_db_name} — {org}",
            "df": org_df,
            "info": {**source_info, "organization": org},
        })
else:
    # single org or no org field — proceed as before
    datasets_to_process.append({...})
```

**b) Batch mode (🚀) — in fetch handler (lines 785-800):**
After fetching each base, check for multi-org:
```python
df = client.fetch_osv_monthly(start_s, end_s)
orgs = OneCClient.extract_organizations(df)
if len(orgs) > 1:
    for org in orgs:
        org_df = df[df["Организация"] == org].copy()
        batch_loaded.append({
            "name": f"{name} — {org}",
            "df": org_df,
            "info": {..., "organization": org},
        })
else:
    batch_loaded.append({"name": name, "df": df, "info": {}})
```

**c) Auto-fill `org_input` from data when single org.**

---

## Backward Compatibility
- Bases without `Organization_Key` → column is null → falls back to single-org mode
- `Catalog_Организации` fetch failure → GUID shown as-is → still works
- No changes to `client_databases.json` format
- No changes to auditor checks — "Организация" column is ignored

## Files Modified
| File | Change |
|---|---|
| `core/api_client.py` | Organization_Key to select, prefetch orgs catalog, _record_organization(), populate column, extract_organizations() |
| `core/auditor.py` | Add "Организация" to OSV_COLUMNS |
| `app/ui.py` | Split multi-org data into separate datasets |

## Verification
- `pytest tests/ -q` — all tests pass
- `ruff check .` — clean
- Single-org base → same behavior as before
- Multi-org base → separate datasets per org
