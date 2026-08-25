"""
Ядро автоаудита бухгалтерских баз 1С:Бухгалтерия (без 1С-интеграции).

Реализует 5 контрольных точек из ТЗ:
  4.1 Красное сальдо
  4.2 Развернутое сальдо
  4.3 Незакрытое сальдо на конец месяца
  4.4 Остатки на счете 000
  4.5 Незакрытые расчеты с контрагентами

Входные данные (ОСВ):
    Период, Счет, Субконто, Тип, НачалоДебет, НачалоКредит,
    ОборотДебет, ОборотКредит, КонецДебет, КонецКредит
Опционально (документы): Дата, Документ, Контрагент, Счет, Вид, Сумма
"""

from __future__ import annotations

from collections.abc import Mapping
import io
from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

from core import ml
from core import nlp
from core.formatting import fmt_date as _fmt_date
from core.formatting import fmt_num as _fmt_num
from core.formatting import fmt_rub as _fmt_rub
from core.formatting import period_sort_series

COLUMN_ALIASES: dict[str, list[str]] = {
    "Счет": ["Счет", "Счёт", "account", "schet"],
    "Субконто": ["Субконто", "sub_account", "Аналитика"],
    "Тип": ["Тип", "type"],
    "Период": ["Период", "period"],
    "НачалоДебет": ["НачалоДебет", "begin_debit", "НД", "start_debit"],
    "НачалоКредит": ["НачалоКредит", "begin_credit", "НК", "start_credit"],
    "ОборотДебет": ["ОборотДебет", "debit_turnover", "ОД"],
    "ОборотКредит": ["ОборотКредит", "credit_turnover", "ОК"],
    "КонецДебет": ["КонецДебет", "end_debit", "СальдоКонецДебет", "КД", "Дебет"],
    "КонецКредит": ["КонецКредит", "end_credit", "СальдоКонецКредит", "КК", "Кредит"],
}

DOCUMENT_ALIASES: dict[str, list[str]] = {
    "Дата": ["Дата", "date"],
    "Документ": ["Документ", "document"],
    "Контрагент": ["Контрагент", "counterparty", "Контрагенты"],
    "Счет": ["Счет", "Счёт", "account"],
    "Вид": ["Вид", "type", "ВидОперации"],
    "Сумма": ["Сумма", "amount", "sum"],
    "Назначение": [
        "Назначение", "Назначение платежа", "НазначениеПлатежа", "purpose",
    ],
}

REQUIRED_OSV: list[str] = ["Счет", "Тип", "КонецДебет", "КонецКредит"]
NUMERIC_OSV: list[str] = [
    "НачалоДебет", "НачалоКредит",
    "ОборотДебет", "ОборотКредит",
    "КонецДебет", "КонецКредит"
]
VALID_TYPES: set[str] = {"A", "P", "AP"}

# Порог, ниже которого сумма считается нулевой (дроби при двойной записи).
EPS: float = 1e-6

# Канонический порядок колонок ОСВ (используется и в normalize_balances,
# и в api_client.fetch_osv, чтобы схемы не расходились).
OSV_COLUMNS: list[str] = [
    "Период", "Счет", "Субконто", "Тип",
    "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
    "КонецДебет", "КонецКредит",
]

DEFAULT_CLOSING_ACCOUNTS: list[str] = ["25", "26", "44", "90", "91", "99"]
SETTLEMENT_GROUPS: list[str] = ["60", "62", "76"]

# Ключи проверок ТЗ для точечного включения (checks: Optional[set[str]]).
CHECK_KEYS: dict[str, str] = {
    "red_balance": "4.1 Красное сальдо",
    "expanded_balance": "4.2 Развернутое сальдо",
    "unclosed_month_end": "4.3 Незакрытое сальдо на конец месяца",
    "account_000": "4.4 Счет 000",
    "settlements": "4.5 Незакрытые расчеты с контрагентами",
}

# Рекомендации по каждой находке (ТЗ п.13). Ключ — точный заголовок находки.
RECOMMENDATIONS: dict[str, str] = {
    "Красное сальдо: активный счет с кредитовым остатком": (
        "Проверьте проводки по счету и корректность начальных остатков. "
        "Отрицательный остаток по активному счету — признак ошибки в учете."
    ),
    "Развернутое сальдо по аналитике": (
        "У одного контрагента/договора одновременно дебетовый и кредитовый остаток. "
        "Проверьте, не требуется ли зачет аванса или сторнирование ошибочной проводки."
    ),
    "Незакрытое сальдо на конец месяца (закрываемые счета)": (
        "Выполните регламентную операцию «Закрытие месяца»: списание расходов "
        "и финансовых результатов. Остаток должен обнулиться."
    ),
    "Зависшее сальдо (не меняется между периодами)": (
        "Остаток не меняется несколько периодов. Проверьте, не «повисли» ли "
        "расчеты/сальдо, которые должны были закрыться."
    ),
    "Незакрытое сальдо на счете 000": (
        "Служебный счет 000 должен быть закрыт после переноса остатков "
        "или ввода начальных остатков. Проверьте корректность ввода остатков."
    ),
    "Контрагенты: аванс и долг одновременно по разным счетам (ОСВ)": (
        "У контрагента одновременно долг (например 60.01) и аванс (60.02). "
        "Рекомендуется провести зачет аванса против долга."
    ),
    "Контрагенты: развернутое сальдо на счетах расчетов (без реестра документов)": (
        "По контрагенту есть одновременно дебетовый и кредитовый остаток. "
        "Загрузите реестр документов для точной проверки или проведите зачет."
    ),
    "Контрагенты: расчеты не закрыты документами": (
        "По контрагенту остались незакрытые расчеты: непогашенный долг, "
        "незачтенный аванс или переплата. Проверьте полноту документов "
        "и при необходимости проведите взаимозачет."
    ),
    "Контрагенты: расхождение документов и остатков ОСВ": (
        "Сумма документов по контрагенту не сходится с остатком ОСВ. "
        "Проверьте полноту выгрузки документов и корректность проводок."
    ),
    "ML: нетипичная сумма операции": (
        "Сумма операции сильно отклоняется от обычных сумм по контрагенту. "
        "Проверьте обоснованность операции и первичные документы."
    ),
    "ML: резкий скачок оборотов между периодами": (
        "Обороты по счету резко изменились между периодами. Проверьте, "
        "связано ли это с разовой операцией или с ошибкой в учете."
    ),
    "ML: возможные дубли контрагентов": (
        "Обнаружены похожие названия контрагентов. Рекомендуется объединить "
        "дубли в справочнике, чтобы не дробить расчеты."
    ),
    "NLP: подозрительные назначения платежей (115-ФЗ)": (
        "Назначение платежа содержит формулировки с повышенным риском "
        "(обнал, нетиповые переводы, займы без договора, расплывчатые "
        "основания). Проверьте операцию и первичные документы."
    ),
    "Контроль групп счетов: незакрытые остатки": (
        "Группа счетов (авансы, товары, денежные средства и т.п.) не закрыта "
        "на конец периода. Проверьте корректность учета и регламентных операций."
    ),
}

# Группы счетов для расширенного контроля 4.3 (включается balance_group_checks=True).
# Код с точкой — конкретный субсчет (точное совпадение), код без точки — вся группа.
GROUP_PRESETS: dict[str, list[str]] = {
    "Авансы выданные/полученные": ["60.02", "62.02", "76.АВ", "76.ВА"],
    "Расходы будущих периодов": ["97"],
    "Товары": ["41", "43"],
    "Денежные средства": ["50", "51", "52", "55", "57", "58"],
    "Кредиты и займы": ["66", "67"],
}

DETAIL_COLUMNS: list[str] = [
    "Проверка", "Уровень", "Период",
    "Счет", "Субконто", "Договор",
    "Дебет", "Кредит",
    "Сумма", "Комментарий"
]

# Уровни находок: машинные значения, порядок важности и русские подписи.
_LEVEL_ORDER: dict[str, int] = {"warning": 1, "error": 2}
_LEVEL_RU: dict[str, str] = {"error": "Ошибка", "warning": "Предупреждение"}

# Разделы печатного экспорта (PDF сейчас, Excel далее). Каждому разделу
# соответствует таблица со своими колонками — без пустых клеток «универсальной
# схемы». Незарегистрированные находки попадают в отдельные разделы автоматически.
SECTION_SPECS: list[tuple[str, tuple[str, ...]]] = [
    ("Закрытие месяца", (
        "Незакрытое сальдо на конец месяца (закрываемые счета)",
        "Зависшее сальдо (не меняется между периодами)",
        "Незакрытое сальдо на счете 000",
        "Контроль групп счетов: незакрытые остатки",
    )),
    ("Красное и развернутое сальдо", (
        "Красное сальдо: активный счет с кредитовым остатком",
        "Развернутое сальдо по аналитике",
    )),
    ("Расчеты с контрагентами", (
        "Контрагенты: аванс и долг одновременно по разным счетам (ОСВ)",
        "Контрагенты: развернутое сальдо на счетах расчетов (без реестра документов)",
        "Контрагенты: расчеты не закрыты документами",
        "Контрагенты: расхождение документов и остатков ОСВ",
    )),
    ("ML: нетипичные суммы операций", ("ML: нетипичная сумма операции",)),
    ("ML: скачки оборотов между периодами", (
        "ML: резкий скачок оборотов между периодами",
    )),
    ("ML: дубли контрагентов", ("ML: возможные дубли контрагентов",)),
    ("NLP: подозрительные назначения платежей (115-ФЗ)", (
        "NLP: подозрительные назначения платежей (115-ФЗ)",
    )),
]


# Оформление колонок печатных таблиц: ширина (относительная) и формат чисел.
_RUB_PDF_COLUMNS: set[str] = {
    "Дебет", "Кредит", "ОборотДебет", "ОборотКредит",
    "Сумма", "Медиана", "НачалоДебет", "НачалоКредит",
}
_NUM_PDF_COLUMNS: set[str] = {"Отклонение", "Отношение", "Сходство"}
_LEVEL_PDF_COLOR: dict[str, tuple[int, int, int]] = {
    "error": (156, 0, 6),
    "warning": (156, 101, 0),
}
_PDF_COL_STYLE: dict[str, tuple[float, str]] = {
    "Проверка": (36, "LEFT"),
    "Период": (17, "LEFT"),
    "Счет": (18, "LEFT"),
    "Субконто": (30, "LEFT"),
    "Дата": (16, "LEFT"),
    "Документ": (24, "LEFT"),
    "Контрагент": (27, "LEFT"),
    "Вид": (13, "LEFT"),
    "Дебет": (18, "RIGHT"),
    "Кредит": (18, "RIGHT"),
    "ОборотДебет": (19, "RIGHT"),
    "ОборотКредит": (19, "RIGHT"),
    "Сумма": (20, "RIGHT"),
    "Медиана": (17, "RIGHT"),
    "Отклонение": (15, "RIGHT"),
    "Отношение": (15, "RIGHT"),
    "Сходство": (15, "RIGHT"),
    "Название А": (28, "LEFT"),
    "Название Б": (28, "LEFT"),
    "Комментарий": (52, "LEFT"),
}

# Денежные колонки в Excel: числовой формат с разделителями тысяч.
_MONEY_COLUMNS: set[str] = {
    "Дебет", "Кредит", "Сумма", "Медиана",
    "НачалоДебет", "НачалоКредит",
    "ОборотДебет", "ОборотКредит",
}
_EXCEL_MONEY_FORMAT = "#,##0.00"

# Сколько крупнейших нарушений показывать в кратком блоке отчета
TOP_FINDINGS_LIMIT = 10

# Версия логики проверок. Пишется в meta каждого прогона и сохраняется в
# истории: находки старых версий могли вычисляться по другим правилам,
# поэтому при экспорте таких записей показывается предупреждение.
AUDIT_LOGIC_VERSION = "1.1"
# Короткие метки находок для колонки «Проверка» объединенных разделов PDF
# (полные заголовки не помещаются в узкую колонку).
_SHORT_PDF_LABELS: dict[str, str] = {
    "Незакрытое сальдо на конец месяца (закрываемые счета)": "Сальдо не закрыто",
    "Зависшее сальдо (не меняется между периодами)": "Зависшее сальдо",
    "Незакрытое сальдо на счете 000": "Счет 000",
    "Контроль групп счетов: незакрытые остатки": "Группа не закрыта",
    "Красное сальдо: активный счет с кредитовым остатком": "Красное сальдо",
    "Развернутое сальдо по аналитике": "Развернутое сальдо",
    "Контрагенты: аванс и долг одновременно по разным счетам (ОСВ)": "Аванс и долг",
    (
        "Контрагенты: развернутое сальдо на счетах расчетов "
        "(без реестра документов)"
    ): "Аванс и долг (без реестра)",
    "Контрагенты: расчеты не закрыты документами": "Расчеты не закрыты",
    "Контрагенты: расхождение документов и остатков ОСВ": "Расхождение с ОСВ",
}


@dataclass
class Finding(Mapping):
    """
    Одна находка контрольной проверки.

    Хранит результат проверки как пары «заголовок — таблица строк».
    Совместим со словарным интерфейсом (e["title"], e["data"], ...),
    который использовался изначально, но дает и атрибутный доступ
    (finding.title) без риска опечаток в ключах.
    """

    level: str
    title: str
    data: pd.DataFrame
    amount: float

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __iter__(self):
        return iter(["level", "title", "data", "amount"])

    def __len__(self) -> int:
        return 4


def account_group(code: object) -> str:
    """
    Группа счета: '60.01' -> '60'; '000' -> '000'
    """

    return str(code).strip().split(".")[0]


def _rename_by_aliases(df: pd.DataFrame, aliases: dict) -> pd.DataFrame:
    rename: dict = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in df.columns:
                rename[name] = canonical
                break
    return df.rename(columns=rename)


def normalize_balances(df: pd.DataFrame) -> pd.DataFrame:
    """
    Приводит ОСВ к канонической схеме и типам
    """

    df = _rename_by_aliases(df, COLUMN_ALIASES).copy()

    missing = [c for c in REQUIRED_OSV if c not in df.columns]
    if missing:
        raise ValueError(
            f"Отсутствуют обязательные колонки: {', '.join(missing)}"
        )

    df["Счет"] = df["Счет"].astype(str).str.strip()
    df["Тип"] = df["Тип"].astype(str).str.strip().str.upper()
    df["Субконто"] = (
        df["Субконто"].fillna("-")
        if "Субконто" in df.columns
        else pd.Series("-", index=df.index)
    ).astype(str)
    df["Договор"] = (
        df["Договор"].fillna('-')
        if "Договор" in df.columns
        else pd.Series('-', index=df.index)
    ).astype(str)
    df["Период"] = (
        df["Период"].fillna("")
        if "Период" in df.columns
        else pd.Series("", index=df.index)
    ).astype(str).str.strip()

    bad_types = set(df["Тип"]) - VALID_TYPES
    if bad_types:
        raise ValueError(
            f"Недопустимые значения Тип: {sorted(bad_types)}. \
            Ожидаются: A, P, AP"
        )

    # Замените блок for col in NUMERIC_OSV: на этот:
    for col in NUMERIC_OSV:
        if col in df.columns:

            clean_series = df[col].astype(str) \
                .str.strip().replace(["-", ""], "0")

            coerced = pd.to_numeric(clean_series, errors="coerce")

            invalid = clean_series.notna() \
                & (clean_series != "nan") \
                & coerced.isna()
            if invalid.any():
                bad = clean_series.loc[invalid].dropna().unique()[:5]
                raise ValueError(
                    f"Колонка '{col}' содержит \
                    нечисловые значения: {list(bad)}"
                )
            df[col] = coerced.fillna(0.0)
        else:
            df[col] = 0.0

    if df.empty:
        raise ValueError("Файл ОСВ пуст")

    return df[[c for c in OSV_COLUMNS if c in df.columns]]


def normalize_documents(df: pd.DataFrame) -> pd.DataFrame:
    """
    Приводит реестр документов к канонической схеме
    """

    df = _rename_by_aliases(df, DOCUMENT_ALIASES).copy()

    missing: list[str] = [
        c
        for c in ["Дата", "Контрагент", "Вид", "Сумма"] if c not in df.columns
    ]
    if missing:
        raise ValueError(f"В файле документов отсутствуют колонки: {', '.join(missing)}")

    df["Контрагент"] = df["Контрагент"].astype(str).str.strip()
    df["Дата"] = pd.to_datetime(df["Дата"], errors="coerce")
    df["Вид"] = df["Вид"].astype(str).str.strip().str.lower()
    df["Сумма"] = pd.to_numeric(df["Сумма"], errors="coerce")

    if "Назначение" in df.columns:
        df["Назначение"] = df["Назначение"].fillna("").astype(str).str.strip()

    if df["Сумма"].isna().any():
        raise ValueError("Колонка 'Сумма' в документах содержит нечисловые значения")

    kind: dict[str, str] = {
        "отгрузка": "отгрузка",
        "реализация": "отгрузка",
        "продажа": "отгрузка",
        "оплата": "оплата",
        "платеж": "оплата",
        "платёж": "оплата",
        "аванс": "аванс",
        "предоплата": "аванс",
    }
    df["ВидНорм"] = df["Вид"].map(kind)
    df.loc[df["ВидНорм"].isna(), "ВидНорм"] = df.loc[df["ВидНорм"].isna(), "Вид"]

    if df.empty:
        raise ValueError("Файл документов пуст")

    return df


class AutoAuditor1C:
    """
    Выполняет контрольные проверки ОСВ по правилам из ТЗ
    """

    def __init__(
        self,
        balances_df: pd.DataFrame,
        documents_df: pd.DataFrame | None = None,
        closing_accounts: list | None = None,
        checks: set[str] | None = None,
        meta: dict | None = None,
        balance_group_checks: bool = False,
        stuck_balance_checks: bool = False,
        ml_enabled: bool = False,
        nlp_enabled: bool = True,
        ml_amount_anomalies: bool = True,
        ml_turnover_jumps: bool = True,
        ml_duplicates: bool = True,
        anomaly_k: float = ml.DEFAULT_K,
        anomaly_min_abs: float = ml.DEFAULT_MIN_ABS,
        anomaly_min_ops: int = ml.DEFAULT_MIN_OPS,
        jump_ratio: float = ml.DEFAULT_JUMP_RATIO,
        jump_min_abs: float = ml.DEFAULT_JUMP_MIN_ABS,
        dup_threshold: int = ml.DEFAULT_SIM_THRESHOLD,
    ) -> None:
        self.balances = normalize_balances(balances_df)
        if documents_df is not None:
            self.documents = normalize_documents(documents_df)
        else:
            self.documents = None
        self.closing_accounts: set[str] = {
            str(a).split(".")[0]
            for a in (closing_accounts or DEFAULT_CLOSING_ACCOUNTS)
        }
        self.ml_enabled = ml_enabled
        self.ml_amount_anomalies = ml_amount_anomalies
        self.ml_turnover_jumps = ml_turnover_jumps
        self.ml_duplicates = ml_duplicates
        self.nlp_enabled = nlp_enabled
        self.anomaly_k = anomaly_k
        self.anomaly_min_abs = anomaly_min_abs
        self.anomaly_min_ops = anomaly_min_ops
        self.jump_ratio = jump_ratio
        self.jump_min_abs = jump_min_abs
        self.dup_threshold = dup_threshold
        self.checks: set[str] | None = checks
        if checks is not None:
            unknown = set(checks) - set(CHECK_KEYS)
            if unknown:
                raise ValueError(
                    f"Неизвестные ключи проверок: {', '.join(sorted(unknown))}"
                )
        self.balance_group_checks = balance_group_checks
        self.stuck_balance_checks = stuck_balance_checks
        self.meta: dict = meta or {}
        self.errors: list[Finding] = []

    #                          ============ Вспомогательные методы ============
    def _check_enabled(self, key: str) -> bool:
        """
        True, если проверка с данным ключом включена (по умолчанию все включены)
        """

        if not CHECK_KEYS.get(key):
            raise ValueError(f"Неизвестный ключ проверки: {key}")
        return self.checks is None or key in self.checks

    def _add(self, level: str, title: str, data: pd.DataFrame) -> None:
        if data.empty:
            return

        if {"КонецДебет", "КонецКредит"} <= set(data.columns):
            if "Развернутое" in title:
                # Для развернутого сальдо берем сумму обеих сторон
                amount = float(data[["КонецДебет", "КонецКредит"]].sum().sum())
            else:
                amount = float((data["КонецДебет"] - data["КонецКредит"]).abs().sum())
        elif "Сумма" in data.columns:
            amount = float(data["Сумма"].fillna(0.0).sum())
        else:
            amount = 0.0
        self.errors.append(Finding(level=level, title=title, data=data, amount=amount))

    @staticmethod
    def _first_occurrence(b: pd.DataFrame, keys: list[str], flag: pd.Series) -> dict[tuple, str]:
        """
        Первый период (по дате), когда flag == True, для каждой группы keys
        """

        b2 = b.loc[flag, keys + ["Период"]].copy()
        b2["_sort"] = pd.to_datetime(b2["Период"], errors="coerce", format="mixed")
        b2 = b2.sort_values(keys + ["_sort"])
        first = b2.groupby(keys, as_index=False)["Период"].first()
        return {
            (row[keys[0]], row[keys[1]]): row["Период"]
            for _, row in first.iterrows()
            if row["Период"]
        }

    def _annotate_since(
        self,
        b: pd.DataFrame,
        flag: pd.Series,
        base_comment: str,
        since_text: str,
    ) -> pd.DataFrame:
        """
        Возвращает строки флага с комментарием о периоде возникновения.

        Например «…; отрицательное сальдо с 31.01.2026» для 4.1.
        """

        sub = b[flag].copy()
        if sub.empty:
            return sub

        since = self._first_occurrence(b, ["Счет", "Субконто"], flag)

        keys = pd.Series(
            list(zip(sub["Счет"], sub["Субконто"])),
            index=sub.index
        )
        periods = keys.map(since)

        # Массовый просмотр комментариев
        sub["Комментарий"] = base_comment
        mask_has_period = periods.notna()
        if mask_has_period.any():
            sub.loc[mask_has_period, "Комментарий"] = (
                base_comment
                + "; "
                + since_text
                + ' '
                + periods[mask_has_period].map(_fmt_date)
            )
        return sub

    #                          ============ 4.1 Красное сальдо ============
    def check_red_balance(self) -> None:
        b = self.balances
        net = b["КонецДебет"] - b["КонецКредит"]
        # Красное сальдо — только отрицательный остаток по активному счету.
        # Дебетовый остаток по пассивному счету ошибкой не считается.
        active = self._annotate_since(
            b,
            (b["Тип"] == "A") & (net < 0),
            "Активный счет имеет кредитовое (отрицательное) сальдо",
            "отрицательное сальдо с",
        )
        if not active.empty:
            self._add("error", "Красное сальдо: активный счет с кредитовым остатком", active)

    #                        ============ 4.2 Развернутое сальдо ============
    def check_expanded_balance(self) -> None:
        b = self.balances
        # Развернутое сальдо — ошибка на уровне аналитики: у одного контрагента/договора
        # одновременно дебетовый и кредитовый остаток. У активно-пассивных счетов без
        # аналитики одновременные Д/К-остатки нормальны (разные контрагенты).
        both = b[
            (b["Субконто"] != "-")
            & (b["КонецДебет"] > 0)
            & (b["КонецКредит"] > 0)
        ].copy()
        both["Комментарий"] = "По контрагенту/аналитике одновременно дебетовое и кредитовое сальдо"
        self._add("warning", "Развернутое сальдо по аналитике", both)

    #                 ============ 4.3 Незакрытое сальдо на конец месяца ============
    def check_unclosed_month_end(self) -> None:
        b = self.balances
        closing = self._annotate_since(
            b,
            (b["Счет"].map(account_group).isin(self.closing_accounts))
            & ((b["КонецДебет"] > 0) | (b["КонецКредит"] > 0)),
            "Остаток по закрываемому счету после закрытия месяца",
            "остаток с",
        )
        if not closing.empty:
            self._add(
                "error",
                "Незакрытое сальдо на конец месяца (закрываемые счета)",
                closing
            )

        # Зависшее сальдо: остаток не меняется между периодами.
        # Опциональная проверка — по умолчанию выключена (stuck_balance_checks).
        # Одна находка на серию одинаковых остатков (а не строка на каждый
        # месяц): период начала серии и длительность в комментарии.
        if not self.stuck_balance_checks:
            return
        b_nonzero = b[(b["КонецДебет"] > 0) | (b["КонецКредит"] > 0)]
        if b_nonzero.empty:
            return
        # Сортируем по хронологии: «Период» бывает в разных форматах,
        # лексикографический порядок ломает границу годов (31.12.2025 vs
        # 01.01.2026), поэтому ключ — распознанная дата.
        b2 = b_nonzero.copy()
        b2["_pkey"] = period_sort_series(b2["Период"])
        b2 = b2.sort_values(
            ["Счет", "Субконто", "_pkey"], kind="stable", na_position="last"
        )

        # Группируем по счету и аналитике, сдвигаем остатки
        grp = b2.groupby(["Счет", "Субконто"], sort=False)
        prev_d = grp["КонецДебет"].shift(1)
        prev_k = grp["КонецКредит"].shift(1)

        # Строка считается зависшей, если текущий остаток
        # точно равен предыдущему
        stuck_mask = (
            ((b2["КонецДебет"] - prev_d).abs() < EPS)
            & ((b2["КонецКредит"] - prev_k).abs() < EPS)
            & prev_d.notna()
        )
        if not stuck_mask.any():
            return

        # Серии подряд идущих зависших строк внутри одной группы:
        # начало серии — зависшая строка, предыдущая строка которой
        # не зависшая (или из другой группы).
        b2["_stuck"] = stuck_mask.to_numpy()
        b2["_prev_period"] = b2["Период"].shift(1)
        prev_stuck = b2["_stuck"].shift(1, fill_value=False)
        same_group = (b2["Счет"] == b2["Счет"].shift(1)) & (
            b2["Субконто"] == b2["Субконто"].shift(1)
        )
        streak_start = b2["_stuck"] & ~(prev_stuck & same_group)
        b2["_streak"] = streak_start.cumsum()

        rows: list[dict[str, Any]] = []
        value_cols = [c for c in b2.columns if not c.startswith("_")]
        for _, streak in b2[b2["_stuck"]].groupby("_streak", sort=False):
            last = streak.iloc[-1]
            n_periods = len(streak) + 1  # + месяц-основание перед серией
            row = {c: last[c] for c in value_cols}
            row["Комментарий"] = (
                "Сальдо не меняется между периодами "
                f"с {_fmt_date(streak.iloc[0]['_prev_period'])} "
                f"({n_periods} пер.)"
            )
            rows.append(row)

        self._add(
            "warning",
            "Зависшее сальдо (не меняется между периодами)",
            pd.DataFrame(rows),
        )

    @staticmethod
    def _matches_group_preset(code: Any, preset: list[str]) -> bool:
        """
        Совпадает ли счет с кодом пресета (код с точкой — точно, без — по группе)
        """

        code = str(code)
        for p in preset:
            if "." in p:
                if code == p:
                    return True
            elif account_group(code) == p:
                return True
        return False

    def check_group_balances(self) -> None:
        """
        Контроль групп счетов (расширенный 4.3): незакрытые остатки групп.

        Для каждой группы из GROUP_PRESETS берется остаток на конец последнего
        периода. Строки агрегируются по родительским счетам (Субконто == "-");
        если их нет в файле — по всем строкам группы (субконто).
        """

        if not self.balance_group_checks:
            return

        b = self.balances
        dates = pd.to_datetime(b["Период"], errors="coerce")

        periods = sorted(set(b.loc[dates.notna(), "Период"]))
        if not periods:
            periods = sorted(set(b["Период"]))
        if not periods:
            return

        last_period = periods[-1]

        rows: list = []
        for group_name, preset in GROUP_PRESETS.items():
            matched = b[
                b["Счет"].map(
                    lambda code: self._matches_group_preset(code, preset)
                )
            ]
            if matched.empty:
                continue

            # Оставляем остатки только по родительским счетам, если они есть
            # Если их нет (выгружены только субсчета), берем детальные строки
            parents = matched[matched["Субконто"] == "-"]
            use = parents if not parents.empty else matched
            g = use[use["Период"] == last_period]
            if g.empty:
                continue

            g_unique = g.drop_duplicates(subset=["Счет", "Субконто"])
            d = float(g_unique["КонецДебет"].sum())
            k = float(g_unique["КонецКредит"].sum())

            net = d - k
            if abs(net) <= EPS:
                continue

            rows.append({
                "Период": last_period,
                "Счет": ", ".join(preset),
                "Субконто": group_name,
                "КонецДебет": d,
                "КонецКредит": k,
                "Сумма": abs(net),
                "Комментарий": (
                    f"Группа «{group_name}» не закрыта на конец периода: остаток "
                    f"{'Д' if net > 0 else 'К'} {_fmt_rub(abs(net))}"
                ),
            })
        if rows:
            res = pd.DataFrame(rows, columns=[
                "Период", "Счет", "Субконто",
                "КонецДебет", "КонецКредит", "Сумма", "Комментарий",
            ])
            self._add("warning", "Контроль групп счетов: незакрытые остатки", res)

    # ============ 4.4 Счет 000 ============
    def check_account_000(self) -> None:
        acc = self.balances[
            (self.balances["Счет"].map(account_group) == "000")
            & ((self.balances["КонецДебет"] > 0) | (self.balances["КонецКредит"] > 0))
        ]
        acc = acc.copy()
        acc["Комментарий"] = "Незакрытый остаток на служебном счете 000"
        self._add("error", "Незакрытое сальдо на счете 000", acc)

    # ============ 4.5 Незакрытые расчеты с контрагентами ============
    def _osv_settlement_balance(self) -> pd.Series:
        b = self.balances
        sett = b[b["Счет"].map(account_group).isin(SETTLEMENT_GROUPS)]
        return (
            sett.groupby("Субконто")["КонецДебет"].sum()
            - sett.groupby("Субконто")["КонецКредит"].sum()
        )

    def _osv_settlement_breakdown(self, subconto: Any) -> dict[str, float]:
        """
        Остаток Д-К по каждому счету расчетов контрагента (например 60.01, 60.02)
        """

        b = self.balances
        sett = b[
            (b["Счет"].map(account_group).isin(SETTLEMENT_GROUPS))
            & (b["Субконто"] == subconto)
        ]
        out: dict[str, float] = {}
        for account, g in sett.groupby("Счет"):
            net = float(g["КонецДебет"].sum() - g["КонецКредит"].sum())
            if abs(net) > EPS:
                out[str(account)] = net
        return out

    def _reference_date(self) -> pd.Timestamp:
        """
        Опорная дата для aging: конец последнего периода ОСВ (или последняя операция)
        """

        dates = pd.to_datetime(self.balances["Период"], errors="coerce").dropna()
        if not dates.empty:
            return dates.max()
        if self.documents is not None:
            dates = pd.to_datetime(self.documents["Дата"], errors="coerce").dropna()
            if not dates.empty:
                return dates.max()
        return pd.Timestamp.today()

    @staticmethod
    def _oldest_unconsumed(
        rows: list[tuple],
        consume: float
    ) -> pd.Timestamp | None:
        """
        FIFO: дата старейшей строки (дата, сумма), остаток которой не погашен.
        Используется только для описания (aging) — не источник новых ошибок.
        """

        rows = [r for r in rows if pd.notna(r[0]) and r[1] > 0]
        rows.sort(key=lambda r: r[0])
        remaining = consume
        for d, amount in rows:
            if remaining < amount - EPS:
                return pd.Timestamp(d)
            remaining -= amount
        return None

    @staticmethod
    def _settlement_components(
        shipped: float, paid: float, advances: float
    ) -> tuple[float, float, float, float]:
        """
        Компоненты незакрытых расчетов по документам контрагента.

        Возвращает (долг, незачтенный аванс, переплата, зачтено):
          долг          = max(отгружено - оплачено - авансы, 0)
          кредит_сторона = max(оплачено + авансы - отгружено, 0)
          незачтенный аванс = min(авансы, кредит_сторона)
          зачтено       = авансы - незачтенный аванс
          переплата     = кредит_сторона - незачтенный аванс
        Сумма компонент всегда равна |отгружено - оплачено - авансы|.
        """

        open_amount = shipped - paid - advances
        debt = max(open_amount, 0.0)
        credit_side = max(-open_amount, 0.0)
        advance_left = min(advances, credit_side)
        overpaid = credit_side - advance_left
        consumed = advances - advance_left
        return debt, advance_left, overpaid, consumed

    def _check_settlement_advance_vs_debt(self) -> None:
        """
        Долг и аванс одновременно на разных счетах расчетов одного контрагента
        (например 60.01 Д + 60.02 К или 62.01 Д + 62.02 К). Проверка 4.2 ловит
        только один счет, поэтому разнёс по разным счетам выделяется здесь.
        Ограничено группами 60 и 62: для 76 разные субсчета могут означать
        независимые расчеты без признака ошибки.
        """

        b = self.balances
        sett = b[
            (b["Счет"].map(account_group).isin(SETTLEMENT_GROUPS))
            & (b["Субконто"] != "-")
        ]
        rows: list = []
        for subconto, g in sett.groupby("Субконто"):
            for group in ("60", "62"):
                gs = g[g["Счет"].map(account_group) == group]
                if gs.empty:
                    continue
                nets = {
                    str(account): float(gg["КонецДебет"].sum() - gg["КонецКредит"].sum())
                    for account, gg in gs.groupby("Счет")
                }
                debit_sum = sum(n for n in nets.values() if n > EPS)
                credit_sum = sum(-n for n in nets.values() if n < -EPS)
                if debit_sum <= EPS or credit_sum <= EPS:
                    continue
                parts = [f"{a} {'Д' if n > 0 else 'К'} {_fmt_rub(abs(n))}" for a, n in sorted(nets.items())]
                rows.append({
                    "Период": "",
                    "Счет": ", ".join(sorted(nets)),
                    "Субконто": subconto,
                    "КонецДебет": debit_sum,
                    "КонецКредит": credit_sum,
                    "Сумма": abs(debit_sum - credit_sum),
                    "Комментарий": (
                        "По контрагенту одновременно дебетовый и кредитовый остаток на разных "
                        f"счетах расчетов (долг и аванс): {'; '.join(parts)}"
                    ),
                })
        if rows:
            res = pd.DataFrame(
                rows,
                columns=[
                    "Период", "Счет", "Субконто",
                    "КонецДебет", "КонецКредит", "Сумма",
                    "Комментарий"
                ],
            )
            self._add(
                "warning",
                "Контрагенты: аванс и долг одновременно по разным счетам (ОСВ)",
                res,
            )

    def check_unclosed_settlements(self) -> None:
        self._check_settlement_advance_vs_debt()

        if self.documents is None:
            sett = self.balances[
                self.balances["Счет"].map(account_group).isin(SETTLEMENT_GROUPS)]
            dup = sett[
                (sett["Субконто"] != "-")
                & (sett["КонецДебет"] > 0)
                & (sett["КонецКредит"] > 0)
            ]
            dup = dup.copy()
            dup["Комментарий"] = "Возможные незакрытые расчеты: аванс и долг по одному контрагенту"
            self._add(
                "warning",
                "Контрагенты: развернутое сальдо на счетах расчетов (без реестра документов)",
                dup
            )
            return

        ref_date = self._reference_date()
        rows: list = []
        for name, g in self.documents.groupby("Контрагент"):
            g = g.sort_values("Дата")
            shipped = float(g.loc[g["ВидНорм"] == "отгрузка", "Сумма"].sum())
            paid = float(g.loc[g["ВидНорм"] == "оплата", "Сумма"].sum())
            advances = float(g.loc[g["ВидНорм"] == "аванс", "Сумма"].sum())
            unknown = g.loc[~g["ВидНорм"].isin(["отгрузка", "оплата", "аванс"])]

            # Компоненты: долг / незачтенный аванс / переплата. Сумма компонент = |open_amount|.
            debt, advance_left, overpaid, consumed = self._settlement_components(
                shipped, paid, advances
            )
            open_amount = debt - advance_left - overpaid

            issues: list[str] = []
            if debt > EPS:
                issues.append(
                    f"остаток долга {_fmt_rub(debt)} (отгружено {_fmt_rub(shipped)}, "
                    f"оплачено {_fmt_rub(paid)}, аванс {_fmt_rub(advances)}, зачтено {_fmt_rub(consumed)})"
                )
            if advance_left > EPS:
                issues.append(
                    f"незачтенный аванс {_fmt_rub(advance_left)} (всего авансов {_fmt_rub(advances)}, "
                    f"зачтено {_fmt_rub(consumed)}, отгружено {_fmt_rub(shipped)})"
                )
            if overpaid > EPS:
                issues.append(
                    f"переплата {_fmt_rub(overpaid)} (оплачено {_fmt_rub(paid)} при отгруженных "
                    f"{_fmt_rub(shipped)} и авансе {_fmt_rub(advances)})"
                )

            # Старейший непогашенный документ (FIFO по датам, описательно)
            # + ожидаемый документ: что должно прийти/пройти, чтобы закрыть остаток
            if debt > EPS:
                oldest = self._oldest_unconsumed(
                    list(
                        g.loc[
                            g["ВидНорм"] == "отгрузка",
                            ["Дата", "Сумма"]
                        ].itertuples(index=False, name=None)
                    ),
                    paid + consumed,
                )
                if oldest is not None:
                    age = (ref_date - oldest).days
                    issues.append(
                        f"старейший непогашенный долг от {_fmt_date(oldest)} (возраст {age} дн.); "
                        f"ожидается оплата по отгрузке от {_fmt_date(oldest)} на {_fmt_rub(debt)}"
                    )
            if advance_left > EPS:
                oldest = self._oldest_unconsumed(
                    list(
                        g.loc[
                            g["ВидНорм"] == "аванс",
                            ["Дата", "Сумма"]
                        ].itertuples(index=False, name=None)
                    ),
                    consumed,
                )
                if oldest is not None:
                    age = (ref_date - oldest).days
                    issues.append(
                        f"старейший незачтенный аванс от {_fmt_date(oldest)} (возраст {age} дн.); "
                        f"ожидается отгрузка/зачет по авансу от {_fmt_date(oldest)} на {_fmt_rub(advance_left)}"
                    )

            # По-счетная разбивка остатков ОСВ (долг/аванс на разных счетах расчетов)
            osv_break = self._osv_settlement_breakdown(name)
            if osv_break:
                accounts = ", ".join(sorted(osv_break))
                issues.append(
                    "ОСВ по счетам: "
                    + "; ".join(
                        f"{a} {'Д' if n > 0 else 'К'} {_fmt_rub(abs(n))}"
                        for a, n in sorted(osv_break.items())
                    )
                )
            else:
                accounts = ", ".join(sorted(set(g["Счет"].astype(str))))

            comment = "; ".join(issues) if issues else "Расчеты закрыты документами"
            if not unknown.empty:
                comment += f"; не распознаны операции: {', '.join(sorted(set(unknown['Вид'])))}"

            osv_net = float(self._osv_settlement_balance().get(name, 0.0))
            if abs(osv_net - open_amount) > 1e-6:
                comment += f"; расхождение с остатком ОСВ ({_fmt_rub(osv_net)})"

            rows.append({
                "Период": "",
                "Счет": accounts,
                "Субконто": name,
                "КонецДебет": max(open_amount, 0.0),
                "КонецКредит": max(-open_amount, 0.0),
                "Сумма": abs(open_amount),
                "Комментарий": comment,
            })

        res = pd.DataFrame(
            rows,
            columns=[
                "Период", "Счет", "Субконто",
                "КонецДебет", "КонецКредит", "Сумма",
                "Комментарий"
            ]
        )
        problems = res[res["Сумма"] > EPS]
        self._add("error", "Контрагенты: расчеты не закрыты документами", problems.copy())

        mismatches = res[
            res["Комментарий"].str.contains("расхождение", na=False)
        ]
        self._add(
            "warning",
            "Контрагенты: расхождение документов и остатков ОСВ",
            mismatches.copy()
        )

    # ============ ML-проверки (аномалии, дубли) ============
    def check_amount_anomalies(self) -> None:
        """
        ML: нетипичная сумма операции по истории контрагента
        """

        if self.documents is None:
            return
        found = ml.detect_amount_anomalies(
            self.documents,
            k=self.anomaly_k,
            min_abs=self.anomaly_min_abs,
            min_ops=self.anomaly_min_ops,
        )
        self._add("warning", "ML: нетипичная сумма операции", found)

    def check_turnover_jumps(self) -> None:
        """
        ML: резкий скачок оборотов между периодами
        """

        found = ml.detect_turnover_jumps(
            self.balances,
            ratio=self.jump_ratio,
            min_abs=self.jump_min_abs,
        )
        self._add("warning", "ML: резкий скачок оборотов между периодами", found)

    def check_duplicate_counterparties(self) -> None:
        """
        ML: нечёткий поиск дублей контрагентов
        """

        names: list[object] = []
        if "Субконто" in self.balances.columns:
            names += list(self.balances["Субконто"].dropna())
        if self.documents is not None and "Контрагент" in self.documents.columns:
            names += list(self.documents["Контрагент"].dropna())
        found = ml.find_duplicate_counterparties(names, threshold=self.dup_threshold)
        self._add("warning", "ML: возможные дубли контрагентов", found)

    def run_ml_checks(self) -> None:
        """
        Запускает включенные ML-проверки
        """

        if not self.ml_enabled:
            return
        if self.ml_amount_anomalies:
            self.check_amount_anomalies()
        if self.ml_turnover_jumps:
            self.check_turnover_jumps()
        if self.ml_duplicates:
            self.check_duplicate_counterparties()

    # ============ NLP-проверки (назначения платежей) ============
    def check_payment_purpose_risks(self) -> None:
        """
        NLP: рискованные формулировки в назначениях платежей (115-ФЗ)
        """

        if self.documents is None:
            return
        found = nlp.detect_payment_risks(self.documents)
        self._add("warning", "NLP: подозрительные назначения платежей (115-ФЗ)", found)

    # ============ Запуск и отчеты ============
    def run_audit(self) -> list[Finding]:
        self.errors = []
        if self._check_enabled("red_balance"):
            self.check_red_balance()
        if self._check_enabled("expanded_balance"):
            self.check_expanded_balance()
        if self._check_enabled("unclosed_month_end"):
            self.check_unclosed_month_end()
            self.check_group_balances()
        if self._check_enabled("account_000"):
            self.check_account_000()
        if self._check_enabled("settlements"):
            self.check_unclosed_settlements()
        self.run_ml_checks()
        if self.nlp_enabled:
            self.check_payment_purpose_risks()
        return self.errors

    @classmethod
    def from_findings(
        cls,
        errors: list[dict],
        meta: dict | None = None,
    ) -> "AutoAuditor1C":
        """
        Восстанавливает аудитора из сериализованных находок (как в истории
        БД) без повторного запуска проверок — только для экспорта отчетов.

        :param errors: список вида [{"title", "level", "amount", "data"}],
            где data — записи таблицы находки (dict или DataFrame)
        :param meta: реквизиты отчета (organization/period/title)
        """

        instance = cls.__new__(cls)
        instance.balances = pd.DataFrame(columns=OSV_COLUMNS)
        instance.documents = None
        instance.meta = dict(meta or {})
        findings: list[Finding] = []
        for e in errors or []:
            data = e.get("data")
            if not isinstance(data, pd.DataFrame):
                data = pd.DataFrame(data or [])
            findings.append(Finding(
                level=str(e.get("level", "warning")),
                title=str(e.get("title", "")),
                data=data,
                amount=float(e.get("amount") or 0.0),
            ))
        instance.errors = findings
        return instance

    def summary_df(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = [
            {
                "Проверка": e["title"],
                "Уровень": e["level"],
                "Строк": len(e["data"]),
                "Сумма": e["amount"],
                "Рекомендации": RECOMMENDATIONS.get(e["title"], ""),
            } for e in self.errors
        ]
        return pd.DataFrame(
            rows,
            columns=["Проверка", "Уровень", "Строк", "Сумма", "Рекомендации"]
        )

    def details_df(self) -> pd.DataFrame:
        rows = []
        for e in self.errors:
            for _, r in e["data"].iterrows():
                rows.append({
                    "Проверка": e["title"],
                    "Уровень": e["level"],
                    "Период": r.get("Период", ""),
                    "Счет": r.get("Счет", ""),
                    "Субконто": r.get("Субконто", ""),
                    "Договор": r.get("Договор", ""),
                    "Дебет": r.get("КонецДебет", 0.0),
                    "Кредит": r.get("КонецКредит", 0.0),
                    "Сумма": (
                        r["Сумма"]
                        if "Сумма" in r.index
                        else abs(float(r.get("КонецДебет", 0.0) - r.get("КонецКредит", 0.0)))
                    ),
                    "Комментарий": r.get("Комментарий", ""),
                })
        return pd.DataFrame(rows, columns=DETAIL_COLUMNS)

    def top_findings_df(self, limit: int = 10) -> pd.DataFrame:
        """
        Крупнейшие нарушения по абсолютной сумме — краткий блок для
        первой страницы PDF и листа «Обзор» в Excel.
        """

        columns = ["Проверка", "Уровень", "Период", "Счет", "Субконто", "Сумма"]
        details = self.details_df()
        if details.empty or limit <= 0:
            return pd.DataFrame(columns=columns)
        ranked = details[columns].copy()
        ranked["_abs"] = ranked["Сумма"].abs()
        ranked = ranked.sort_values("_abs", ascending=False).head(limit)
        return ranked.drop(columns="_abs").reset_index(drop=True)

    def _sections(self) -> list[dict[str, Any]]:
        """
        Находки, сгруппированные в разделы экспорта (см. SECTION_SPECS).

        Каждый раздел: {"title", "level", "recommendation", "df"} — таблица df
        со «своими» колонками (сальдовые находки приводятся к единому виду
        Период/Счет/Субконто/Дебет/Кредит/Сумма/Комментарий; ML и NLP проходят
        с нативными колонками). Полностью пустые колонки скрываются, а в
        объединенных разделах «Проверка» печатается короткой меткой
        (_SHORT_PDF_LABELS). Разделы без находок не возвращаются.
        """

        by_title: dict[str, Finding] = {e["title"]: e for e in self.errors}
        used: set[str] = set()
        sections: list[dict[str, Any]] = []

        specs = list(SECTION_SPECS)
        spec_titles = {t for _, titles in SECTION_SPECS for t in titles}

        for sec_title, titles in specs:
            findings = []
            for t in titles:
                e = by_title.get(t)
                if e is not None:
                    findings.append(e)
                    used.add(t)
            if not findings:
                continue
            frames: list[pd.DataFrame] = []
            merged_col = len(findings) > 1
            for e in findings:
                data = e["data"]
                if "КонецДебет" in data.columns:
                    label = _SHORT_PDF_LABELS.get(e["title"], e["title"]) \
                        if merged_col else None
                    frames.append(self._project_saldo(data, label))
                else:
                    frames.append(data.copy())
            recs = " ".join(
                dict.fromkeys(
                    RECOMMENDATIONS.get(e["title"], "") for e in findings
                    if RECOMMENDATIONS.get(e["title"], "")
                )
            )
            level = max((e["level"] for e in findings), key=lambda lv: _LEVEL_ORDER.get(lv, 0))
            frame = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
            sections.append({
                "title": sec_title,
                "level": level,
                "recommendation": recs,
                "df": self._drop_empty_columns(frame),
            })

        for e in self.errors:
            if e["title"] not in used and e["title"] not in spec_titles:
                data = e["data"]
                if "КонецДебет" in data.columns:
                    data = self._project_saldo(data, None)
                else:
                    data = data.copy()
                sections.append({
                    "title": e["title"],
                    "level": e["level"],
                    "recommendation": RECOMMENDATIONS.get(e["title"], ""),
                    "df": self._drop_empty_columns(data),
                })
        return sections

    @staticmethod
    def _drop_empty_columns(df: pd.DataFrame) -> pd.DataFrame:
        """
        Убирает колонки, где нет ни одного непустого значения
        (например «Период» у контрагентских находок без привязки к месяцу).
        """

        if df.empty:
            return df
        keep: list[str] = []
        col_idx = 0
        cols = list(df.columns)
        while col_idx < len(cols):
            series = df[cols[col_idx]]
            non_empty = series.notna() & (
                series.astype(str).str.strip().ne("")
                & series.astype(str).ne("nan")
            )
            if bool(non_empty.any()):
                keep.append(cols[col_idx])
            col_idx += 1
        return df[keep]

    @staticmethod
    def _project_saldo(data: pd.DataFrame, check_label: str | None) -> pd.DataFrame:
        """
        Приводит сальдовую находку (колонки ОСВ или готовые КонецДебет/
        КонецКредит) к единому табличному виду раздела.
        """

        rows: list[dict[str, Any]] = []
        for _, r in data.iterrows():
            d = float(r.get("КонецДебет", 0.0) or 0.0)
            k = float(r.get("КонецКредит", 0.0) or 0.0)
            amount = r.get("Сумма")
            amount = abs(d - k) if amount is None or pd.isna(amount) else float(amount)
            row: dict[str, Any] = {
                "Период": r.get("Период", ""),
                "Счет": r.get("Счет", ""),
                "Субконто": r.get("Субконто", ""),
                "Дебет": d,
                "Кредит": k,
                "Сумма": round(amount, 2),
                "Комментарий": r.get("Комментарий", ""),
            }
            if check_label is not None:
                row = {"Проверка": check_label, **row}
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def _account_codes_in(cell: object) -> list[str]:
        """
        Разбирает значение «Счет» (возможно «51, 62.01») на отдельные коды
        """

        return [
            c.strip()
            for c in str(cell).replace(";", ",").split(",")
            if c.strip()
        ]

    def accounts_with_errors(self) -> list[str]:
        """
        Отсортированный список счетов, по которым есть нарушения
        """

        codes: set[str] = set()
        details = self.details_df()
        if not details.empty:
            for cell in details["Счет"].dropna():
                codes.update(self._account_codes_in(cell))
        return sorted(codes)

    def account_subaccounts(self, account_code: str) -> list[str]:
        """
        Субсчета счета: '60' -> ['60.01', '60.02', ...], '60.01' -> ['60.01']
        """

        code = str(account_code).strip()
        if "." in code:
            return [code]
        if self.balances is not None and "Счет" in self.balances.columns:
            values = self.balances["Счет"].dropna()
            codes: set[str] = set()
            idx = 0
            while idx < len(values):
                item = str(values.iloc[idx]).strip()
                if account_group(item) == code:
                    codes.add(item)
                idx += 1
            if codes:
                return sorted(codes)
        return [code]

    def account_report_df(self, account_code: str) -> pd.DataFrame:
        """
        Все нарушения выбранного счета одним списком: для родительского
        счета учитываются и строки его субсчетов («хождение по субсчетам»)
        """

        details = self.details_df()
        if details.empty:
            return details
        subaccounts = set(self.account_subaccounts(account_code))
        return details[
            details["Счет"].map(
                lambda cell: bool(set(self._account_codes_in(cell)) & subaccounts)
            )
        ]

    def account_subconto(self, account_code: str) -> list[str]:
        """
        Субконто/контрагенты, задействованные по счету (из ОСВ и документов)
        """

        names: set[str] = set()
        subaccounts = self.account_subaccounts(account_code)
        if "Счет" in self.balances.columns and "Субконто" in self.balances.columns:
            rows = self.balances[self.balances["Счет"].isin(subaccounts)]["Субконто"]
            names.update(
                str(n).strip() for n in rows.dropna() if str(n).strip() != "-"
            )
        if (
            self.documents is not None
            and "Счет" in self.documents.columns
            and "Контрагент" in self.documents.columns
        ):
            rows = self.documents[self.documents["Счет"].isin(subaccounts)]["Контрагент"]
            names.update(
                str(n).strip() for n in rows.dropna() if str(n).strip()
            )
        return sorted(names)

    def account_subconto_duplicates(
        self,
        account_code: str,
        threshold: int | None = None,
    ) -> pd.DataFrame:
        """
        ML-поиск возможных дублей контрагентов внутри выбранного счета
        """

        names = self.account_subconto(account_code)
        if not names:
            return pd.DataFrame(columns=[
                "Субконто", "Название А",
                "Название Б", "Сходство",
                "Комментарий"
            ])
        return ml.find_duplicate_counterparties(
            names, threshold=threshold if threshold is not None else self.dup_threshold
        )

    def accounts_summary_df(self) -> pd.DataFrame:
        """
        Сводка по счетам для листа «По счетам» в экспорте Excel/PDF
        """

        columns: list[str] = [
            "Счет", "Кол-во нарушений",
            "Проверки", "Периоды",
            "Сумма", "Дубли контрагентов"
        ]
        details = self.details_df()
        if details.empty:
            return pd.DataFrame(columns=columns)
        rows = []
        for account in self.accounts_with_errors():
            g = self.account_report_df(account)
            checks = sorted(set(g["Проверка"].astype(str)))
            periods = sorted(p for p in set(g["Период"].astype(str)) if p)
            rows.append({
                "Счет": account,
                "Кол-во нарушений": len(g),
                "Проверки": "; ".join(checks),
                "Периоды": ", ".join(periods),
                "Сумма": round(float(g["Сумма"].sum()), 2),
                "Дубли контрагентов": len(self.account_subconto_duplicates(account)),
            })
        return pd.DataFrame(rows, columns=columns)

    def _meta_payload(self) -> dict:
        """
        Реквизиты отчета (организация/период/заголовок) из self.meta
        """

        payload: dict = {}
        for key in ("title", "organization", "period"):
            value = self.meta.get(key)
            if value:
                payload[key] = value
        return payload

    def report(self) -> dict:
        result = {
            "total_flags": sum(len(e["data"]) for e in self.errors),
            "total_amount": sum(e["amount"] for e in self.errors),
            "summary": self.summary_df(),
            "details": self.details_df()
        }
        result.update(self._meta_payload())
        if not self.errors:
            result.update(status="ok", status_label="Успешно")
        else:
            result.update(status="warning", status_label="Есть ошибки")
        return result

    def to_excel(self, account_pass: dict | None = None) -> bytes:
        """
        Отчет Excel: сначала краткий лист «Обзор» (реквизиты, статус, сводка
        по проверкам, крупнейшие нарушения), затем полные данные — «Сводный
        отчет», «Детальный отчет», «По счетам» и опционально «Проход по
        счетам» (ТЗ п.6, 14). Шапки закреплены, включен автофильтр, колонки
        подобраны по ширине, деньги — в числовом формате.

        :param account_pass: результат автопрохода по счетам (см. core.account_pass)
            — добавляет лист «Проход по счетам».
        """

        from openpyxl.styles import Alignment, Font, PatternFill

        summary = self.summary_df()
        details = self.details_df()
        by_account = self.accounts_summary_df()
        top_findings = self.top_findings_df(TOP_FINDINGS_LIMIT)

        pass_details = pd.DataFrame()
        if account_pass is not None:
            pd_ = account_pass.get("details_df")
            if pd_ is not None and not getattr(pd_, "empty", True):
                pass_details = pd_

        n_err = sum(len(e["data"]) for e in self.errors if e["level"] == "error")
        if n_err:
            status_label = "Есть ошибки"
        elif self.errors:
            status_label = "Есть предупреждения"
        else:
            status_label = "Успешно"

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            # ---------- Лист «Обзор»: кратко и по делу ----------
            meta_payload = self._meta_payload()
            meta_rows = [
                {"Параметр": "Заголовок", "Значение": meta_payload.get("title", "")},
                {"Параметр": "Организация", "Значение": meta_payload.get("organization", "")},
                {"Параметр": "Период", "Значение": meta_payload.get("period", "")},
                {"Параметр": "Сформировано",
                 "Значение": pd.Timestamp.now().strftime("%d.%m.%Y %H:%M")},
                {"Параметр": "Статус", "Значение": status_label},
            ]
            pd.DataFrame(meta_rows).to_excel(
                writer, sheet_name="Обзор", index=False
            )

            summary.to_excel(writer, sheet_name="Сводный отчет", index=False)
            details.to_excel(writer, sheet_name="Детальный отчет", index=False)
            by_account.to_excel(writer, sheet_name="По счетам", index=False)
            if not pass_details.empty:
                pass_details.to_excel(writer, sheet_name="Проход по счетам", index=False)

            wb = writer.book
            header_fill = PatternFill("solid", fgColor="4472C4")
            header_font = Font(color="FFFFFF", bold=True)
            red_fill = PatternFill("solid", fgColor="FFC7CE")
            yellow_fill = PatternFill("solid", fgColor="FFEB9C")
            title_font = Font(bold=True, size=12)
            header_align = Alignment(horizontal="center", vertical="top", wrap_text=True)

            def _finish_table_sheet(ws) -> None:
                """Шапка, закрепление строки, автофильтр, ширины и деньги."""

                for cell in ws[1]:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = header_align
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions
                self._excel_fit_widths(ws)
                self._excel_money_formats(ws)

            overview = writer.sheets["Обзор"]
            blocks: list[tuple[int, pd.DataFrame]] = []

            def _add_block(cursor: int, title: str, frame: pd.DataFrame) -> int:
                """Блок «заголовок + таблица» на «Обзоре»; возвращает курсор."""

                overview.cell(row=cursor + 1, column=1, value=title).font = title_font
                header_row = cursor + 2  # 1-based строка шапки таблицы
                frame.to_excel(
                    writer, sheet_name="Обзор", index=False, startrow=cursor + 1
                )
                blocks.append((header_row, frame))
                return cursor + 2 + len(frame) + 1

            cursor = len(meta_rows) + 2  # отступ после блока параметров (0-based)
            if not summary.empty:
                cursor = _add_block(cursor, "Сводка по проверкам", summary)
            if not top_findings.empty:
                cursor = _add_block(cursor, "Крупнейшие нарушения", top_findings)

            # Оформление «Обзора»: параметры, шапки блоков, уровни, деньги
            overview["A1"].fill = header_fill
            overview["B1"].fill = header_fill
            overview["A1"].font = header_font
            overview["B1"].font = header_font
            for i in range(2, len(meta_rows) + 1):
                overview.cell(row=i, column=1).font = Font(bold=True)
            for header_row, frame in blocks:
                for j in range(1, len(frame.columns) + 1):
                    cell = overview.cell(row=header_row, column=j)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = header_align
                if "Уровень" in frame.columns:
                    lvl_j = list(frame.columns).index("Уровень") + 1
                    for i in range(len(frame)):
                        level = overview.cell(
                            row=header_row + 1 + i, column=lvl_j
                        ).value
                        fill = (
                            red_fill if level == "error"
                            else (yellow_fill if level == "warning" else None)
                        )
                        if fill is None:
                            continue
                        for j in range(1, len(frame.columns) + 1):
                            overview.cell(
                                row=header_row + 1 + i, column=j
                            ).fill = fill
                for j, h in enumerate(frame.columns, start=1):
                    if str(h) in _MONEY_COLUMNS:
                        for row_cells in overview.iter_rows(
                            min_row=header_row + 1,
                            max_row=header_row + len(frame),
                            min_col=j,
                            max_col=j,
                        ):
                            for cell in row_cells:
                                cell.number_format = _EXCEL_MONEY_FORMAT

            # ---------- Оформление листов с данными ----------
            sheets_to_color: list[tuple] = [
                ("Сводный отчет", 1),
                ("Детальный отчет", 1),
            ]
            if not pass_details.empty:
                # Колонка «Уровень» третья: перед ней «Счет» из автопрохода.
                sheets_to_color.append(("Проход по счетам", 2))

            for sheet, level_col in sheets_to_color:
                ws = wb[sheet]
                _finish_table_sheet(ws)
                for row in ws.iter_rows(min_row=2):
                    level = row[level_col].value
                    if level == "error":
                        fill = red_fill
                    elif level == "warning":
                        fill = yellow_fill
                    else:
                        continue
                    for cell in row:
                        cell.fill = fill

            _finish_table_sheet(wb["По счетам"])

        return buf.getvalue()

    @staticmethod
    def _excel_fit_widths(ws, cap: int = 55) -> None:
        """
        Подбирает ширину колонок по содержимому (с ограничением сверху)
        """

        from openpyxl.utils import get_column_letter

        widths: dict[int, int] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                length = len(str(cell.value))
                if length > widths.get(cell.column, 0):
                    widths[cell.column] = length
        for idx, width in widths.items():
            ws.column_dimensions[get_column_letter(idx)].width = \
                min(cap, max(9, width + 2))

    @staticmethod
    def _excel_money_formats(ws) -> None:
        """
        Числовой формат для денежных колонок листа
        """

        from openpyxl.utils import get_column_letter

        for cell in ws[1]:
            if str(cell.value) in _MONEY_COLUMNS:
                letter = get_column_letter(cell.column)
                for row_cells in ws[f"{letter}2:{letter}{ws.max_row}"]:
                    for c in row_cells:
                        c.number_format = _EXCEL_MONEY_FORMAT

    # ============ PDF-отчет (ТЗ п.6.2) ============
    @staticmethod
    def _find_pdf_font() -> str | None:
        """
        Ищет TTF-шрифт с кириллицей: встроенный DejaVu, затем системные (Windows/Linux).
        """

        import os

        bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans.ttf")
        candidates = [
            bundled,
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\times.ttf",
            r"C:\Windows\Fonts\DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None


    @staticmethod
    def _register_pdf_fonts(pdf, font_path: str) -> bool:
        """
        Обычный и (если найден) жирный вариант шрифта с кириллицей
        """

        import os as _os

        pdf.add_font("DejaVu", "", font_path)
        candidates = [
            _os.path.join(_os.path.dirname(font_path), "DejaVuSans-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        for path in candidates:
            if _os.path.exists(path):
                pdf.add_font("DejaVu", "B", path)
                return True
        return False

    @staticmethod
    def _pdf_table(
        pdf,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        widths: Sequence[float],
        has_bold: bool = False,
    ) -> None:
        """
        Таблица с переносом длинного текста в ячейках и повтором шапки на
        новых страницах (fpdf2.table). Денежные колонки выровнены вправо в
        российском формате, коэффициенты — с одним знаком после запятой.
        Ширины колонок масштабируются под доступную ширину страницы.
        """

        from fpdf.fonts import FontFace

        usable = pdf.w - pdf.l_margin - pdf.r_margin
        total = float(sum(widths)) or 1.0
        col_widths = [w / total * usable for w in widths]

        aligns: list[str] = []
        fmts: list = []
        for h in headers:
            if h in _RUB_PDF_COLUMNS:
                aligns.append("RIGHT")
                fmts.append(_fmt_rub)
            elif h in _NUM_PDF_COLUMNS:
                aligns.append("RIGHT")
                fmts.append(_fmt_num)
            elif h in ("Дата", "Период"):
                aligns.append("LEFT")
                fmts.append(_fmt_date)
            else:
                aligns.append("LEFT")
                fmts.append(lambda v: "" if v is None else str(v))

        headings_style = FontFace(
            fill_color=(235, 238, 242), emphasis="BOLD" if has_bold else None
        )

        pdf.set_font("DejaVu", size=8)
        with pdf.table(
            col_widths=tuple(col_widths),
            text_align=tuple(aligns),
            line_height=4.4,
            padding=1.2,
            headings_style=headings_style,
            cell_fill_color=(249, 250, 251),
            cell_fill_mode="ROWS",
        ) as table:
            # Первая строка таблицы fpdf2 считает шапкой (headings_style)
            hr = table.row()
            for h in headers:
                hr.cell(h)
            for row in rows:
                tr = table.row()
                for i, v in enumerate(row):
                    tr.cell(fmts[i](v))
        pdf.ln(2)

    def to_pdf(self, account_pass: dict | None = None) -> bytes:
        """
        Печатный отчет (ТЗ п.6.2): шапка с реквизитами и статусом, итоги по
        разделам, таблицы разделов со «своими» колонками (без пустых клеток
        универсальной схемы), рекомендации внутри разделов, автопроход по
        счетам. Длинный текст не обрезается — переносится; шапки таблиц
        повторяются на новых страницах; внизу — номер страницы.

        :param account_pass: результат автопрохода по счетам (см. core.account_pass)
            — добавляет раздел «Автопроход по счетам».

        Требует fpdf2 и шрифт с кириллицей (встроенный fonts/DejaVuSans.ttf).
        """

        try:
            from fpdf import FPDF
            from fpdf.enums import XPos, YPos
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise ImportError(
                "PDF-экспорт требует библиотеку fpdf2. Установите: pip install fpdf2"
            ) from exc

        font_path = self._find_pdf_font()
        if font_path is None:  # pragma: no cover - зависит от окружения
            raise RuntimeError(
                "Не найден шрифт с кириллицей для PDF. Проверьте наличие "
                "fonts/DejaVuSans.ttf рядом с auditor.py."
            )

        class _AuditPdf(FPDF):
            """
            A4-документ с нумерацией страниц в нижнем колонтитуле
            """

            def footer(self) -> None:
                self.set_y(-12)
                self.set_font("DejaVu", size=8)
                self.set_text_color(130, 130, 130)
                self.cell(0, 6, f"Стр. {self.page_no()}", align="C")

        pdf = _AuditPdf(format="A4")
        pdf.set_margins(10, 10, 10)
        has_bold = self._register_pdf_fonts(pdf, font_path)
        pdf.set_auto_page_break(auto=True, margin=16)
        pdf.add_page()

        meta = self._meta_payload()

        # ---------- Шапка ----------
        pdf.set_font("DejaVu", size=14, style="B" if has_bold else "")
        pdf.cell(
            0, 8,
            meta.get("title", "Отчет аудита бухгалтерских баз 1С"),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        pdf.ln(2)
        pdf.set_font("DejaVu", size=10)
        pdf.cell(
            0, 6, f"Организация: {meta.get('organization', '—')}",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.cell(
            0, 6, f"Период: {meta.get('period', '—')}",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.cell(
            0, 6, f"Дата отчета: {pd.Timestamp.now().strftime('%d.%m.%Y %H:%M')}",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

        sections = self._sections()
        n_err_rows = int(sum(
            len(e["data"]) for e in self.errors if e["level"] == "error"
        ))
        n_warn_rows = int(sum(
            len(e["data"]) for e in self.errors if e["level"] == "warning"
        ))
        counts = []
        if n_err_rows:
            counts.append(f"Ошибок: {n_err_rows}")
        if n_warn_rows:
            counts.append(f"Предупреждений: {n_warn_rows}")
        pdf.cell(0, 6, " · ".join(counts) if counts else "Нарушений не выявлено",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        status = (
            "Есть ошибки" if n_err_rows
            else ("Есть предупреждения" if n_warn_rows else "Успешно")
        )
        status_rgb = (
            (156, 0, 6) if n_err_rows
            else ((156, 101, 0) if n_warn_rows else (0, 128, 0))
        )
        pdf.set_text_color(*status_rgb)
        pdf.set_font("DejaVu", size=10, style="B" if has_bold else "")
        pdf.cell(0, 6, f"Статус: {status}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)

        if not sections:
            pdf.ln(4)
            pdf.set_text_color(0, 128, 0)
            pdf.cell(0, 7, "Нарушений не выявлено.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
            return bytes(pdf.output())

        #                ========== Кратко: сводка по проверкам ==========
        pdf.ln(4)
        summary = self.summary_df()
        if not summary.empty:
            pdf.set_text_color(68, 114, 196)
            pdf.set_font("DejaVu", size=11, style="B" if has_bold else "")
            pdf.cell(
                0, 7, "Сводка по проверкам",
                new_x=XPos.LMARGIN, new_y=YPos.NEXT
            )
            pdf.set_text_color(0, 0, 0)
            sum_rows = [
                [
                    _SHORT_PDF_LABELS.get(str(r["Проверка"]), str(r["Проверка"])),
                    _LEVEL_RU.get(str(r["Уровень"]), str(r["Уровень"])),
                    int(r["Строк"]),
                    float(r["Сумма"]),
                ]
                for _, r in summary.iterrows()
            ]
            self._pdf_table(
                pdf,
                ["Проверка", "Уровень", "Строк", "Сумма"],
                sum_rows,
                [64, 26, 14, 24],
                has_bold=has_bold,
            )

            #              ========== Крупнейшие нарушения ==========
            top = self.top_findings_df(TOP_FINDINGS_LIMIT)
            if not top.empty:
                pdf.ln(2)
                pdf.set_text_color(68, 114, 196)
                pdf.set_font("DejaVu", size=11, style="B" if has_bold else "")
                pdf.cell(
                    0, 7, "Крупнейшие нарушения",
                    new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                )
                pdf.set_text_color(0, 0, 0)
                top_rows = [
                    [
                        _SHORT_PDF_LABELS.get(str(r["Проверка"]), str(r["Проверка"])),
                        r["Период"],
                        r["Счет"],
                        r["Субконто"],
                        float(r["Сумма"]),
                    ]
                    for _, r in top.iterrows()
                ]
                self._pdf_table(
                    pdf,
                    ["Проверка", "Период", "Счет", "Субконто", "Сумма"],
                    top_rows,
                    [30, 16, 12, 34, 20],
                    has_bold=has_bold,
                )

        #                   ========== Разделы с находками ==========
        for s in sections:
            df = s["df"]
            if pdf.get_y() > 230:
                pdf.add_page()
            pdf.ln(3)
            pdf.set_text_color(*_LEVEL_PDF_COLOR.get(s["level"], (0, 0, 0)))
            pdf.set_font("DejaVu", size=11, style="B" if has_bold else "")
            pdf.cell(0, 7, s["title"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(95, 95, 95)
            pdf.set_font("DejaVu", size=8.5)
            if s["recommendation"]:
                pdf.multi_cell(0, 4.2, s["recommendation"])
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1)

            headers = list(df.columns)
            widths = [_PDF_COL_STYLE.get(h, (20, "LEFT"))[0] for h in headers]
            rows = [[v for v in rec] for rec in df.to_numpy()]
            self._pdf_table(pdf, headers, rows, widths, has_bold=has_bold)

        # ---------- Автопроход по счетам ----------
        pass_details = pd.DataFrame()
        pass_dups = pd.DataFrame()
        if account_pass is not None:
            details = account_pass.get("details_df")
            dups = account_pass.get("duplicates_df")
            if details is not None:
                pass_details = details
            if dups is not None:
                pass_dups = dups

        if not pass_details.empty:
            if pdf.get_y() > 200:
                pdf.add_page()
            else:
                pdf.ln(4)
            pdf.set_text_color(68, 114, 196)
            pdf.set_font("DejaVu", size=11, style="B" if has_bold else "")
            pdf.cell(0, 7, "Автопроход по счетам",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)

            pass_headers = ["Проверка", "Период", "Субконто", "Дебет", "Кредит", "Сумма"]
            pass_widths = [
                _PDF_COL_STYLE[h][0] for h in pass_headers
            ]
            accounts = sorted(set(str(c) for c in pass_details["Счет"].dropna()))
            for account in accounts:
                acc_rows = pass_details[pass_details["Счет"].astype(str) == account]
                dups_n = 0
                if not pass_dups.empty and "Счет" in pass_dups.columns:
                    dups_n = int((pass_dups["Счет"].astype(str) == account).sum())
                note = f"Счет {account} — нарушений: {len(acc_rows)}"
                if dups_n:
                    note += f", дублей контрагентов: {dups_n}"
                pdf.set_font("DejaVu", size=10, style="B" if has_bold else "")
                pdf.cell(0, 6, note, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                table_rows = [
                    [r.get("Проверка", ""), r.get("Период", ""),
                     r.get("Субконто", ""), r.get("Дебет", 0.0),
                     r.get("Кредит", 0.0), r.get("Сумма", 0.0)]
                    for _, r in acc_rows.iterrows()
                ]
                self._pdf_table(pdf, pass_headers, table_rows, pass_widths, has_bold=has_bold)

        return bytes(pdf.output())
