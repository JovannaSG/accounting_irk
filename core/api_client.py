import logging
import os
import sys
import threading
import concurrent.futures
from typing import Any

import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

from core.auditor import OSV_COLUMNS
from core.loaders import PLAN_OF_ACCOUNTS, _infer_type

logger = logging.getLogger(__name__)

_OSV_FIELDS: dict[str, list[str]] = {
    "НачалоДебет": ["СуммаOpeningBalanceDr", "ОстатокДт", "СНД", "НачалоДебет"],
    "НачалоКредит": ["СуммаOpeningBalanceCr", "ОстатокКт", "СНК", "НачалоКредит"],
    "ОборотДебет": ["СуммаTurnoverDr", "ОборотДт", "ОД", "ОборотДебет"],
    "ОборотКредит": ["СуммаTurnoverCr", "ОборотКт", "ОК", "ОборотКредит"],
    "КонецДебет": ["СуммаClosingBalanceDr", "ОстатокДтКонеч", "СКД", "КонецДебет"],
    "КонецКредит": ["СуммаClosingBalanceCr", "ОстатокКтКонеч", "СКК", "КонецКредит"],
}

_OSV_SELECT: str = ",".join(
    ["Account_Key"]
    + [candidates[0] for candidates in _OSV_FIELDS.values()]
)


class OneCClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({'Accept': 'application/json'})
        
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        self._code_by_key: dict[str, str] = {}
        
        # НОВЫЙ КЭШ: Локальная база для перевода GUID -> Название
        self._guid_to_name: dict[str, str] = {}
        self._catalogs_loaded = False
        self._cache_lock = threading.Lock()

    def _paginate(self, endpoint: str, params: dict[str, Any]) -> list[dict]:
        all_records: list[dict] = []
        while True:
            try:
                response = self.session.get(endpoint, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.HTTPError as e:
                raise ValueError(self._friendly_http_error(e)) from e
            except requests.exceptions.RequestException as e:
                raise ValueError(f"Не удалось соединиться с 1C: {e}") from e

            chunk = data.get('value', [])
            if not chunk:
                break

            all_records.extend(chunk)

            if len(chunk) < params.get("$top", 1000):
                break
            params["$skip"] = params.get("$skip", 0) + params["$top"]

        return all_records

    @staticmethod
    def _friendly_http_error(error: requests.exceptions.HTTPError) -> str:
        status: int | None  = None
        url: str = ""
        if error.response is not None:
            status = error.response.status_code
            url = error.response.url

        if status == 401:
            return "OData вернул 401 Unauthorized."
        if status in (400, 404) and "BalanceAndTurnovers" in url:
            return "OData не нашел виртуальную таблицу регистра бухгалтерии."
        return f"OData-запрос завершился ошибкой {status}: {url}"

    # ================= ЗАГРУЗКА СПРАВОЧНИКОВ (CLIENT-SIDE JOIN) =================
    def _prefetch_catalogs(self) -> None:
        if self._catalogs_loaded:
            return
            
        catalogs = [
            "Catalog_Контрагенты",
            "Catalog_ДоговорыКонтрагентов",
            "Catalog_ФизическиеЛица"
        ]
        
        logger.info("Предзагрузка справочников (Контрагенты, Договоры, Физлица) для аналитики...")
        
        def load_cat(cat_name: str):
            endpoint = f"{self.base_url}/odata/standard.odata/{cat_name}"
            params = {"$format": "json", "$select": "Ref_Key,Description", "$top": 2000}
            try:
                recs = self._paginate(endpoint, params)
                with self._cache_lock:
                    for r in recs:
                        k = r.get("Ref_Key")
                        d = r.get("Description")
                        if k and d:
                            self._guid_to_name[str(k)] = str(d)
            except Exception as e:
                logger.debug(f"Пропуск справочника {cat_name}: {e}")

        # Грузим 3 справочника параллельно
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(load_cat, catalogs))
            
        self._catalogs_loaded = True
        logger.info(f"Справочники загружены. В кэше {len(self._guid_to_name)} записей.")

    def fetch_chart_of_accounts(self) -> dict[str, str]:
        if self._code_by_key:
            return self._code_by_key

        endpoint = f"{self.base_url}/odata/standard.odata/ChartOfAccounts_Хозрасчетный"
        params = {"$format": "json", "$select": "Ref_Key,Code", "$top": 1000, "$skip": 0}
        records = self._paginate(endpoint, params)

        with self._cache_lock:
            for rec in records:
                key = rec.get("Ref_Key") or rec.get("Ссылка")
                code = rec.get("Code") or rec.get("Код")
                if key and code:
                    self._code_by_key[str(key)] = str(code)
        return self._code_by_key

    def _get_account_guid(self, target_code: str) -> str | None:
        if not self._code_by_key:
            self.fetch_chart_of_accounts()
            
        for guid, code in self._code_by_key.items():
            if str(code).strip() == str(target_code).strip():
                return guid
        return None

    @staticmethod
    def _record_account_code(rec: dict, code_by_key: dict[str, str]) -> str | None:
        acct = rec.get("Счет") or rec.get("Account")
        if isinstance(acct, dict):
            code = acct.get("Code") or acct.get("Код")
            if code:
                return str(code)
        key = rec.get("Account_Key") or rec.get("Счет_Key")
        if key and str(key) in code_by_key:
            return code_by_key[str(key)]
        return None

    # Расшифровка GUID по локальному кэшу
    def _record_subconto(self, rec: dict) -> str:
        val = rec.get("ExtDimension1")
        if not val:
            return "-"
        if isinstance(val, dict):
            return val.get("Description") \
                or val.get("Наименование") \
                or val.get("Name") \
                or str(val)

        val_str = str(val)
        return self._guid_to_name.get(val_str, val_str)

    def _record_contract(self, rec: dict) -> str:
        val = rec.get("ExtDimension2")
        if not val:
            return "-"
        if isinstance(val, dict):
            return val.get("Description") \
                or val.get("Наименование") \
                or val.get("Name") \
                or str(val)
            
        val_str = str(val)
        return self._guid_to_name.get(val_str, val_str)

    def _records_to_osv(
        self,
        records: list[dict],
        period_end: str,
        account_code: str | None = None
    ) -> pd.DataFrame:
        if not records:
            return pd.DataFrame(columns=OSV_COLUMNS)

        if any(not self._record_account_code(r, {}) for r in records):
            self.fetch_chart_of_accounts()
        code_by_key = self._code_by_key

        rows: list[dict[str, Any]] = []
        for rec in records:
            acc_code = self._record_account_code(rec, code_by_key) or "?"
            if account_code is not None and acc_code != account_code:
                continue
            row: dict[str, Any] = {
                "Период": period_end,
                "Счет": acc_code,
                "Субконто": self._record_subconto(rec),
                "Договор": self._record_contract(rec),
            }
            for target, candidates in _OSV_FIELDS.items():
                value = None
                for key in candidates:
                    if key in rec:
                        value = rec[key]
                        break
                row[target] = float(value) if value is not None else 0.0
            rows.append(row)

        if not rows:
            return pd.DataFrame(columns=OSV_COLUMNS)

        by_code: dict[str, list[dict]] = {}
        for r in rows:
            by_code.setdefault(r["Счет"], []).append(r)

        for code, code_rows in by_code.items():
            parent_code = code.split('.')[0] if '.' in code else code
            t = _infer_type(parent_code, PLAN_OF_ACCOUNTS, code_rows)
            for r in code_rows:
                r["Тип"] = t

        return pd.DataFrame(rows, columns=OSV_COLUMNS)

    def fetch_osv(self, period_start: str, period_end: str) -> pd.DataFrame:
        register: str = "AccountingRegister_Хозрасчетный"
        method: str = f"BalanceAndTurnovers(StartPeriod=datetime'{period_start}', EndPeriod=datetime'{period_end}')"
        endpoint: str = f"{self.base_url}/odata/standard.odata/{register}/{method}"

        params: dict[str, Any] = {
            "$format": "json",
            "$select": _OSV_SELECT,
            "$top": 1000,
            "$skip": 0,
        }
        records = self._paginate(endpoint, params)
        if not records:
            return pd.DataFrame(columns=OSV_COLUMNS)
        return self._records_to_osv(records, period_end)

    def fetch_osv_account_subconto(self, period_start: str, period_end: str, account_code: str) -> pd.DataFrame:
        register: str = "AccountingRegister_Хозрасчетный"
        guid = self._get_account_guid(account_code)
        
        if guid:
            method = (
                f"BalanceAndTurnovers(StartPeriod=datetime'{period_start}', "
                f"EndPeriod=datetime'{period_end}', "
                f"AccountCondition='Account_Key eq guid''{guid}''')"
            )
        else:
            method = f"BalanceAndTurnovers(StartPeriod=datetime'{period_start}', EndPeriod=datetime'{period_end}')"

        endpoint: str = f"{self.base_url}/odata/standard.odata/{register}/{method}"

        # Запрашиваем голые GUID-ы субконто, без $expand (Это спасает 1С от падения)
        base_select: list[str] = ["Account_Key", "ExtDimension1", "ExtDimension2"]
        for candidates in _OSV_FIELDS.values():
            base_select.append(candidates[0])

        params = {
            "$format": "json",
            "$select": ",".join(base_select),
            "$top": 1000,
            "$skip": 0,
        }
        
        try:
            records = self._paginate(endpoint, params)
        except Exception as e:
            logger.error(f"Ошибка загрузки счета {account_code}: {e}")
            records = []

        if not records:
            return pd.DataFrame(columns=OSV_COLUMNS)

        return self._records_to_osv(records, period_end, account_code)

    @staticmethod
    def _month_ranges(period_start: str, period_end: str):
        import calendar
        start = pd.to_datetime(period_start, errors="coerce")
        end = pd.to_datetime(period_end, errors="coerce")
        
        cursor = start.normalize().replace(day=1, hour=0, minute=0, second=0)
        while cursor <= end:
            month_end = cursor.replace(
                day=calendar.monthrange(cursor.year, cursor.month)[1],
                hour=23, minute=59, second=59,
            )
            period_end_actual = min(month_end, end)
            yield (
                cursor.strftime("%Y-%m-%dT00:00:00"),
                period_end_actual.strftime("%Y-%m-%dT23:59:59"),
            )
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)

    def fetch_osv_monthly(
        self, 
        period_start: str, 
        period_end: str, 
        target_accounts: list[str] | None = None
    ) -> pd.DataFrame:
        
        _DETAILED_ACCOUNTS = ("60", "62", "76", "71", "73", "58", "66", "67")
        if pd.to_datetime(period_start) > pd.to_datetime(period_end):
            raise ValueError("Некорректный диапазон дат.")
            
        frames: list[pd.DataFrame] = []
        month_ranges = list(self._month_ranges(period_start, period_end))
        
        # Разогрев кэша (План счетов + Справочники контрагентов и договоров)
        self.fetch_chart_of_accounts()
        self._prefetch_catalogs()

        agg_dfs_by_month = {}
        logger.info(f"Сборка сводной базы за {len(month_ranges)} мес...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_to_month = {
                executor.submit(self.fetch_osv, m_start, m_end): (m_start, m_end)
                for m_start, m_end in month_ranges
            }
            for future in concurrent.futures.as_completed(future_to_month):
                m_range = future_to_month[future]
                try:
                    agg_dfs_by_month[m_range] = future.result()
                except Exception as e:
                    agg_dfs_by_month[m_range] = pd.DataFrame(columns=OSV_COLUMNS)

        detailed_tasks = []
        for m_range in month_ranges:
            agg_df = agg_dfs_by_month[m_range]
            if agg_df.empty:
                continue
                
            active_accounts = agg_df["Счет"].dropna().unique().tolist()
            for acc in active_accounts:
                acc_str = str(acc)
                needs_detail = False
                
                if target_accounts:
                    if any(acc_str.startswith(t) for t in target_accounts):
                        needs_detail = True
                else:
                    if acc_str.startswith(_DETAILED_ACCOUNTS):
                        needs_detail = True
                        
                if needs_detail:
                    detailed_tasks.append({
                        "m_range": m_range,
                        "acc_str": acc_str,
                        "agg_df": agg_df
                    })
                else:
                    frames.append(agg_df[agg_df["Счет"] == acc_str])

        if detailed_tasks:
            logger.info(f"Точечные запросы аналитики ({len(detailed_tasks)} шт.)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_to_task = {
                    executor.submit(
                        self.fetch_osv_account_subconto, 
                        task["m_range"][0], 
                        task["m_range"][1], 
                        task["acc_str"]
                    ): task
                    for task in detailed_tasks
                }
                
                for future in concurrent.futures.as_completed(future_to_task):
                    task = future_to_task[future]
                    acc_str = task["acc_str"]
                    agg_df = task["agg_df"]
                    
                    try:
                        det_df = future.result()
                        if not det_df.empty:
                            mask = agg_df["Счет"] == acc_str
                            if mask.any():
                                det_df["Тип"] = agg_df[mask]["Тип"].tolist()[0]
                            frames.append(det_df)
                        else:
                            frames.append(agg_df[agg_df["Счет"] == acc_str])
                    except Exception:
                        frames.append(agg_df[agg_df["Счет"] == acc_str])

        if not frames:
            return pd.DataFrame(columns=OSV_COLUMNS)
            
        df = pd.concat(frames, ignore_index=True)
        return df.reindex(columns=OSV_COLUMNS)