import logging
import os
import sys
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

import pandas as pd

# разрешаем запуск как скрипта: python core/api_client.py
if __package__ in (None, ""):
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

from core.auditor import OSV_COLUMNS
from core.loaders import PLAN_OF_ACCOUNTS, _infer_type

logger = logging.getLogger(__name__)

# Кандидаты на имена полей виртуальной таблицы регистра бухгалтерии.
# Точный состав полей OData-запросов к BalanceAndTurnovers официально не
# документирован, поэтому имена резолвятся гибко (первый найденный ключ).
_OSV_FIELDS: dict[str, list[str]] = {
    "НачалоДебет": ["СуммаOpeningBalanceDr", "ОстатокДт", "СНД", "НачалоДебет"],
    "НачалоКредит": ["СуммаOpeningBalanceCr", "ОстатокКт", "СНК", "НачалоКредит"],
    "ОборотДебет": ["СуммаTurnoverDr", "ОборотДт", "ОД", "ОборотДебет"],
    "ОборотКредит": ["СуммаTurnoverCr", "ОборотКт", "ОК", "ОборотКредит"],
    "КонецДебет": ["СуммаClosingBalanceDr", "ОстатокДтКонеч", "СКД", "КонецДебет"],
    "КонецКредит": ["СуммаClosingBalanceCr", "ОстатокКтКонеч", "СКК", "КонецКредит"],
}

# Поля, запрашиваемые у BalanceAndTurnovers. 1С группирует строки виртуальной
# таблицы по выбранным полям: без $select возвращаются тысячи строк в разрезе
# организаций/субконто, а с ним — одна строка на счёт. Поэтому select обязателен.
_OSV_SELECT: str = ",".join(
    ["Account_Key"]
    + [candidates[0] for candidates in _OSV_FIELDS.values()]
)


class OneCClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        """
        Инициализируйте клиент 1С API.

        Работает как для самостоятельной опубликованной базы данных, так и для 1С:Fresh.
        Для Fresh base_url - это URL-адрес приложения, взятый из браузера
        (без указания языка), например https://1cfresh.com/a/sbm_demo/1962515
        :параметр base_url: Базовый URL-адрес приложения 1С.
        """

        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        # Принуждаем 1С вернуть json, а не xml
        self.session.headers.update({'Accept': 'application/json'})
        self._code_by_key: dict[str, str] = {}

    #                           ========== Общие методы ==========
    def _paginate(self, endpoint: str, params: dict[str, Any]) -> list[dict]:
        """
        Загружает все страницы стандартного интерфейса OData ($top/$skip)
        """

        all_records: list[dict] = []
        while True:
            try:
                response = self.session.get(
                    endpoint,
                    params=params,
                    timeout=30
                )
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.HTTPError as e:
                raise ValueError(self._friendly_http_error(e, params)) from e
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
            return (
                "OData вернул 401 Unauthorized. Проверьте логин/пароль служебного "
                "пользователя (роль «УдаленныйДоступOData» или «Полные права»). "
                "Если в базе есть ограничения доступа к данным, добавьте в запрос "
                "параметр allowedOnly=true. URL запроса: "
                + url
            )
        if status in (400, 404) and "BalanceAndTurnovers" in url:
            return (
                "OData не нашел виртуальную таблицу регистра бухгалтерии. Убедитесь, "
                "что в обработке «Настройка стандартного интерфейса OData» (вкладка "
                "«Состав») включен доступ к регистру бухгалтерии «Хозрасчетный». URL: "
                + url
            )
        return f"OData-запрос завершился ошибкой {status}: {url}"

    #                      ========== Движения регистра бухгалтерии ==========
    def fetch_accounting_records(
        self,
        period_start: str,
        period_end: str
    ) -> pd.DataFrame:
        """
        Извлекает бухгалтерские записи (регистратор бухгалтерии)
        за определенный период.
        Возвращает одну строку для каждой проводки (перемещения), а не OSV
        """

        endpoint: str = f"{self.base_url}/odata/standard.odata/AccountingRegister_Хозрасчетный"

        params: dict[str, Any] = {
            "$format": "json",
            "$filter": f"Period ge datetime'{period_start}' and Period le datetime'{period_end}'",
            # Разверните связанные таблицы,
            # чтобы получить читаемые имена
            # вместо необработанных идентификаторов GUID
            "$expand": "AccountDr, AccountCr, ExtDimension1Dr, ExtDimension1Cr",
            # 1С обычно ограничивает количество ответов 1000 строками.
            # Нам нужно извлечь все
            "$top": 1000,
            "$skip": 0,
        }

        logger.info("Загрузка движений регистра бухгалтерии из 1C...")
        records = self._paginate(endpoint, params)
        return self._flatten_and_clean(records)

    def _flatten_and_clean(self, records: list) -> pd.DataFrame:
        """
        Сглаживает глубоко вложенный JSON из 1С в 2D-фрейм данных Pandas.
        """

        if not records:
            logger.info("Движения за указанный период не найдены.")
            return pd.DataFrame()

        rows: list[dict] = []
        for rec in records:
            acc_dr = rec.get("AccountDr") or {}
            acc_cr = rec.get("AccountCr") or {}

            rows.append({
                "Дата": rec.get("Period"),
                "Дебет": acc_dr.get("Code") if isinstance(acc_dr, dict) else None,
                "Кредит": acc_cr.get("Code") if isinstance(acc_cr, dict) else None,
                "Сумма": rec.get("Сумма", rec.get("Amount", 0.0)),
                "Документ": rec.get("Recorder_Type")
            })
        df = pd.DataFrame()
        return df

    #           ========== ОСВ через виртуальную таблицу регистра бухгалтерии ==========
    def fetch_chart_of_accounts(self) -> dict[str, str]:
        """
        Возвращает справочник GUID -> код счета (план счетов «Хозрасчетный»).
        Используется, когда в ответе регистра приходит только Счет_Key (GUID).
        """

        if self._code_by_key:
            return self._code_by_key

        endpoint = f"{self.base_url}/odata/standard.odata/ChartOfAccounts_Хозрасчетный"
        params: dict[str, Any] = {
            "$format": "json",
            "$select": "Ref_Key,Code",
            "$top": 1000,
            "$skip": 0
        }
        records = self._paginate(endpoint, params)

        for rec in records:
            key = rec.get("Ref_Key") or rec.get("Ссылка")
            code = rec.get("Code") or rec.get("Код")
            if key and code:
                self._code_by_key[str(key)] = str(code)
        return self._code_by_key

    @staticmethod
    def _record_account_code(
        rec: dict,
        code_by_key: dict[str, str]
    ) -> str | None:
        acct = rec.get("Счет") or rec.get("Account")
        if isinstance(acct, dict):
            code = acct.get("Code") or acct.get("Код")
            if code:
                return str(code)

        key = rec.get("Account_Key") or rec.get("Счет_Key")
        if key and str(key) in code_by_key:
            return code_by_key[str(key)]
        return None

    def _record_subconto(self, rec: dict) -> str:
        """
        Представление субконто (контрагента) из ExtDimension1 записи регистра
        """

        subconto_val = rec.get("ExtDimension1")
        if isinstance(subconto_val, dict):
            return (
                subconto_val.get("Description")
                or subconto_val.get("Наименование")
                or subconto_val.get("Name")
                or subconto_val.get("Code")
                or subconto_val.get("Код")
                or str(subconto_val)
            )
        if subconto_val:
            return str(subconto_val)
        return "-"

    def _records_to_osv(
        self,
        records: list[dict],
        period_end: str,
        account_code: str | None = None,
    ) -> pd.DataFrame:
        """
        Собирает DataFrame со схемой OSV_COLUMNS из записей BalanceAndTurnovers.

        При account_code не равном None строки других счетов отбрасываются
        (серверный $filter по Account_Key OData запрещает, поэтому фильтрация
        клиентская).
        """

        if not records:
            return pd.DataFrame(columns=OSV_COLUMNS)

        # Если пришёл только GUID счета — подгружаем план счетов для расшифровки.
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

        # Тип (A/P/AP) — по типовому плану счетов 1С 8.3, эвристика по остаткам.
        by_code: dict[str, list[dict]] = {}
        for r in rows:
            by_code.setdefault(r["Счет"], []).append(r)

        for code, code_rows in by_code.items():
            if '.' in code:
                parent_code = code.split('.')[0]
            else:
                parent_code = code

            t = _infer_type(parent_code, PLAN_OF_ACCOUNTS, code_rows)
            for r in code_rows:
                r["Тип"] = t

        return pd.DataFrame(rows, columns=OSV_COLUMNS)

    def fetch_osv(self, period_start: str, period_end: str) -> pd.DataFrame:
        """
        Fetches the balance sheet (ОСВ) for a period via the virtual table
        BalanceAndTurnovers of the accounting register.

        Returns a DataFrame with the standard OSV schema:
        Период, Счет, Субконто, Тип, НачалоДебет, НачалоКредит,
        ОборотДебет, ОборотКредит, КонецДебет, КонецКредит
        """

        register: str = "AccountingRegister_Хозрасчетный"
        method: str = (
            f"BalanceAndTurnovers(StartPeriod=datetime'{period_start}', "
            f"EndPeriod=datetime'{period_end}')"
        )
        endpoint: str = f"{self.base_url}/odata/standard.odata/{register}/{method}"

        params: dict[str, Any] = {
            "$format": "json",
            "$select": _OSV_SELECT,
            "$top": 1000,
            "$skip": 0,
        }

        logger.info(f"Загрузка ОСВ из 1C ({period_start} ... {period_end})...")
        records = self._paginate(endpoint, params)
        if not records:
            logger.info("ОСВ за указанный период не найдена.")
            return pd.DataFrame(columns=OSV_COLUMNS)
        return self._records_to_osv(records, period_end)

    @staticmethod
    def _month_ranges(period_start: str, period_end: str):
        """
        Генератор (start_str, end_str) по календарным месяцам диапазона.

        Начало = первое число месяца 00:00:00, конец = последнее число месяца
        23:59:59 (для последнего месяца — period_end). Формат входа/выхода:
        'YYYY-MM-DDTHH:MM:SS'.
        """

        import calendar

        start = pd.to_datetime(period_start, errors="coerce")
        end = pd.to_datetime(period_end, errors="coerce")
        if pd.isna(start) or pd.isna(end) or start > end:
            raise ValueError(
                f"Некорректный диапазон периода ОСВ: {period_start} ... {period_end}"
            )

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
    ) -> pd.DataFrame:
        """
        ОСВ по месяцам диапазона [period_start, period_end].

        Отдельный запрос на каждый календарный месяц (StartPeriod = начало
        месяца, EndPeriod = конец месяца, для последнего месяца — period_end).
        В результате каждая строка получает Период = конец своего месяца, что
        даёт несколько периодов — работают проверки «зависшее сальдо» (4.3)
        и ML «скачки оборотов».

        Пустой результат — пустой DataFrame со схемой OSV_COLUMNS.
        """

        frames: list[pd.DataFrame] = []
        for month_start_str, month_end_str in self._month_ranges(
            period_start,
            period_end
        ):
            frames.append(self.fetch_osv(month_start_str, month_end_str))

        if not frames:
            return pd.DataFrame(columns=OSV_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        # Гарантируем схему даже если некоторые столбцы пустые
        return df.reindex(columns=OSV_COLUMNS)

    def fetch_osv_account_subconto(
        self,
        period_start: str,
        period_end: str,
        account_code: str,
    ) -> pd.DataFrame:
        """
        Извлекает подробную OSV с помощью subconto для конкретной учетной записи.
        Запрашивает виртуальную таблицу с включенным расширением ExtDimension1.
        Фильтрует по коду учетной записи на стороне клиента,
        поскольку фильтрация на стороне сервера по ключу учетной записи запрещена
        """

        register: str = "AccountingRegister_Хозрасчетный"
        method: str = (
            f"BalanceAndTurnovers(StartPeriod=datetime'{period_start}', "
            f"EndPeriod=datetime'{period_end}')"
        )
        endpoint: str = f"{self.base_url}/odata/standard.odata/{register}/{method}"

        # Выбираем Account_Key, ExtDimension1 и числовые поля
        select_fields: list[str] = [
            "Account_Key",
            "ExtDimension1",
        ]
        for candidates in _OSV_FIELDS.values():
            select_fields.append(candidates[0])

        select_str = ",".join(select_fields)

        # Сначала запуск с помощью $expand=ExtDimension1
        records: list[dict] = []
        try:
            params: dict = {
                "$format": "json",
                "$select": select_str,
                "$expand": "ExtDimension1",
                "$top": 1000,
                "$skip": 0,
            }
            records = self._paginate(endpoint, params)
        except Exception as exc:  # noqa: BLE001 - старые конфигурации не знают $expand
            logger.warning(
                "Запрос с $expand=ExtDimension1 не удался (%s); повторяем без $expand",
                exc,
            )
            # Запасной вариант без $expand
            params: dict = {
                "$format": "json",
                "$select": select_str,
                "$top": 1000,
                "$skip": 0,
            }
            records = self._paginate(endpoint, params)

        if not records:
            return pd.DataFrame(columns=OSV_COLUMNS)

        return self._records_to_osv(records, period_end, account_code)

    def fetch_osv_account_subconto_monthly(
        self,
        period_start: str,
        period_end: str,
        account_code: str,
    ) -> pd.DataFrame:
        """
        Отчёт по счёту с субконто по месяцам диапазона [period_start, period_end].

        Аналог fetch_osv_monthly, но в разрезе аналитики выбранного счёта:
        отдельный запрос на каждый календарный месяц (см. fetch_osv_account_subconto).
        Период каждой строки = конец её месяца, поэтому работают ML-проверки,
        требующие историю за 2+ периода (аномалии сумм, скачки оборотов).

        Пустой результат — пустой DataFrame со схемой OSV_COLUMNS.
        """

        frames: list[pd.DataFrame] = []
        for month_start_str, month_end_str in self._month_ranges(
            period_start,
            period_end
        ):
            frames.append(
                self.fetch_osv_account_subconto(
                    month_start_str,
                    month_end_str,
                    account_code
                )
            )

        if not frames:
            return pd.DataFrame(columns=OSV_COLUMNS)

        df = pd.concat(frames, ignore_index=True)
        # Гарантируем схему
        return df.reindex(columns=OSV_COLUMNS)


def _cli() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Получение ОСВ из 1C (в т.ч. 1C:Fresh) через OData"
    )
    parser.add_argument(
        "url", nargs="?",
        default=os.environ.get("ONEC_URL", "https://msk1.1cfresh.com/a/ea/3418453"),
        help="URL приложения, например https://1cfresh.com/a/sbm_demo/1962515"
    )
    parser.add_argument(
        "user", nargs="?",
        default=os.environ.get("ONEC_USER", "odata.user")
    )
    parser.add_argument(
        "password", nargs="?",
        default=os.environ.get("ONEC_PASS", "")
    )
    parser.add_argument("--start", default="2026-01-01T00:00:00")
    parser.add_argument("--end", default="2026-06-30T23:59:59")
    args = parser.parse_args()

    client = OneCClient(args.url, args.user, args.password)
    df = client.fetch_osv(args.start, args.end)
    print("\nProcessed OSV ready for AI Auditor:")
    if df.empty:
        print("(нет данных)")
    else:
        print(df.head(20).to_string())
        print(f"\nСчетов: {len(df)}, строк: {len(df.index)}")


if __name__ == "__main__":
    _cli()
