"""
Ядро автоаудита бухгалтерских баз 1С:Бухгалтерия (без 1С-интеграции).

Реализует 5 контрольных точек из ТЗ:
  4.1 Красное сальдо
  4.2 Развернутое сальдо
  4.3 Незакрытое сальдо на конец месяца
  4.4 Остатки на счете 000
  4.5 Незакрытые расчеты с контрагентами

Входные данные (ОСВ):  Период, Счет, Субконто, Тип, НачалоДебет, НачалоКредит,
                        ОборотДебет, ОборотКредит, КонецДебет, КонецКредит
Опционально (документы): Дата, Документ, Контрагент, Счет, Вид, Сумма
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from core import ml

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
    "Счет", "Субконто",
    "Дебет", "Кредит",
    "Сумма", "Комментарий"
]


@dataclass
class Finding:
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
        return iter((self.level, self.title, self.data, self.amount))

    def __len__(self) -> int:
        return 4

    def keys(self) -> tuple[str, str, str, str]:
        return ("level", "title", "data", "amount")

    def values(self):
        return (getattr(self, key) for key in self.keys())

    def items(self):
        return ((key, getattr(self, key)) for key in self.keys())

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "title": self.title,
            "data": self.data,
            "amount": self.amount
        }


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
        raise ValueError(f"Отсутствуют обязательные колонки: {', '.join(missing)}")

    df["Счет"] = df["Счет"].astype(str).str.strip()
    df["Тип"] = df["Тип"].astype(str).str.strip().str.upper()
    df["Субконто"] = (
        df["Субконто"].fillna("-")
        if "Субконто" in df.columns
        else pd.Series("-", index=df.index)
    ).astype(str)
    df["Период"] = (
        df["Период"].fillna("")
        if "Период" in df.columns
        else pd.Series("", index=df.index)
    ).astype(str).str.strip()

    bad_types = set(df["Тип"]) - VALID_TYPES
    if bad_types:
        raise ValueError(f"Недопустимые значения Тип: {sorted(bad_types)}. Ожидаются: A, P, AP")

    for col in NUMERIC_OSV:
        if col in df.columns:
            coerced = pd.to_numeric(df[col], errors="coerce")
            invalid = df[col].notna() & coerced.isna()
            if invalid.any():
                bad = df.loc[invalid, col].dropna().unique()[:5]
                raise ValueError(f"Колонка '{col}' содержит нечисловые значения: {list(bad)}")
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

    missing = [c for c in ["Дата", "Контрагент", "Вид", "Сумма"] if c not in df.columns]
    if missing:
        raise ValueError(f"В файле документов отсутствуют колонки: {', '.join(missing)}")

    df["Контрагент"] = df["Контрагент"].astype(str).str.strip()
    df["Дата"] = pd.to_datetime(df["Дата"], errors="coerce")
    df["Вид"] = df["Вид"].astype(str).str.strip().str.lower()
    df["Сумма"] = pd.to_numeric(df["Сумма"], errors="coerce")

    if df["Сумма"].isna().any():
        raise ValueError("Колонка 'Сумма' в документах содержит нечисловые значения")
    df["Сумма"] = df["Сумма"].fillna(0.0)

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
        documents_df: Optional[pd.DataFrame] = None,
        closing_accounts: Optional[list] = None,
        checks: Optional[set[str]] = None,
        meta: Optional[dict] = None,
        balance_group_checks: bool = False,
        ml_enabled: bool = False,
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
        self.documents = normalize_documents(documents_df) if documents_df is not None else None
        self.closing_accounts: set[str] = {
            str(a).split(".")[0]
            for a in (closing_accounts or DEFAULT_CLOSING_ACCOUNTS)
        }
        self.ml_enabled = ml_enabled
        self.ml_amount_anomalies = ml_amount_anomalies
        self.ml_turnover_jumps = ml_turnover_jumps
        self.ml_duplicates = ml_duplicates
        self.anomaly_k = anomaly_k
        self.anomaly_min_abs = anomaly_min_abs
        self.anomaly_min_ops = anomaly_min_ops
        self.jump_ratio = jump_ratio
        self.jump_min_abs = jump_min_abs
        self.dup_threshold = dup_threshold
        self.checks: Optional[set[str]] = checks
        if checks is not None:
            unknown = set(checks) - set(CHECK_KEYS)
            if unknown:
                raise ValueError(
                    f"Неизвестные ключи проверок: {', '.join(sorted(unknown))}"
                )
        self.balance_group_checks = balance_group_checks
        self.meta: dict = meta or {}
        self.errors: list[Finding] = []

    # ============ Вспомогательные методы ============
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

        Например «…; отрицательное сальдо с 2026-01-31» для 4.1.
        """

        sub = b[flag].copy()
        if sub.empty:
            return sub
        since = self._first_occurrence(b, ["Счет", "Субконто"], flag)

        def _make(r: pd.Series) -> str:
            period = since.get((r["Счет"], r["Субконто"]))
            return f"{base_comment}; {since_text} {period}" if period else base_comment

        sub["Комментарий"] = sub.apply(_make, axis=1)
        return sub

    # ============ 4.1 Красное сальдо ============
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

    # ============ 4.2 Развернутое сальдо ============
    def check_expanded_balance(self) -> None:
        b = self.balances
        # Развернутое сальдо — ошибка на уровне аналитики: у одного контрагента/договора
        # одновременно дебетовый и кредитовый остаток. У активно-пассивных счетов без
        # аналитики одновременные Д/К-остатки нормальны (разные контрагенты).
        both = b[
            (b["Субконто"] != "-")
            & (b["КонецДебет"] > 0)
            & (b["КонецКредит"] > 0)
        ]
        both["Комментарий"] = "По контрагенту/аналитике одновременно дебетовое и кредитовое сальдо"
        self._add("warning", "Развернутое сальдо по аналитике", both)

    # ============ 4.3 Незакрытое сальдо на конец месяца ============
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

        # Зависшее сальдо: остаток не меняется между периодами
        b2 = b.sort_values(["Счет", "Субконто", "Период"])
        prev_d = b2.groupby(["Счет", "Субконто"])["КонецДебет"].shift(1)
        prev_k = b2.groupby(["Счет", "Субконто"])["КонецКредит"].shift(1)
        stuck = b2[
            ((b2["КонецДебет"] > 0) | (b2["КонецКредит"] > 0))
            & (b2["КонецДебет"] == prev_d)
            & (b2["КонецКредит"] == prev_k)
            & prev_d.notna()
        ]
        stuck = stuck.copy()
        stuck["Комментарий"] = "Сальдо не меняется между периодами (зависший остаток)"
        self._add(
            "warning",
            "Зависшее сальдо (не меняется между периодами)",
            stuck
        )

    @staticmethod
    def _matches_group_preset(code: str, preset: list[str]) -> bool:
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
            matched = b[b["Счет"].map(lambda code: self._matches_group_preset(code, preset))]
            if matched.empty:
                continue
            parents = matched[matched["Субконто"] == "-"]
            use = parents if not parents.empty else matched
            g = use[use["Период"] == last_period]
            if g.empty:
                continue
            d = float(g["КонецДебет"].sum())
            k = float(g["КонецКредит"].sum())
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
                    f"{'Д' if net > 0 else 'К'} {abs(net):,.2f}"
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

    def _osv_settlement_breakdown(self, subconto: str) -> dict[str, float]:
        """Остаток Д-К по каждому счету расчетов контрагента (например 60.01, 60.02)."""
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
    def _oldest_unconsumed(rows: list[tuple], consume: float) -> Optional[pd.Timestamp]:
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
                parts = [f"{a} {'Д' if n > 0 else 'К'} {abs(n):,.2f}" for a, n in sorted(nets.items())]
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
                    f"остаток долга {debt:,.2f} (отгружено {shipped:,.2f}, "
                    f"оплачено {paid:,.2f}, аванс {advances:,.2f}, зачтено {consumed:,.2f})"
                )
            if advance_left > EPS:
                issues.append(
                    f"незачтенный аванс {advance_left:,.2f} (всего авансов {advances:,.2f}, "
                    f"зачтено {consumed:,.2f}, отгружено {shipped:,.2f})"
                )
            if overpaid > EPS:
                issues.append(
                    f"переплата {overpaid:,.2f} (оплачено {paid:,.2f} при отгруженных "
                    f"{shipped:,.2f} и авансе {advances:,.2f})"
                )

            # Aging: старейший непогашенный документ (FIFO по датам, описательно)
            # + ожидаемый документ: что должно прийти/пройти, чтобы закрыть остаток
            if debt > EPS:
                oldest = self._oldest_unconsumed(
                    list(g.loc[g["ВидНорм"] == "отгрузка", ["Дата", "Сумма"]].itertuples(index=False, name=None)),
                    paid + consumed,
                )
                if oldest is not None:
                    age = (ref_date - oldest).days
                    issues.append(
                        f"старейший непогашенный долг от {oldest.date()} (возраст {age} дн.); "
                        f"ожидается оплата по отгрузке от {oldest.date()} на {debt:,.2f}"
                    )
            if advance_left > EPS:
                oldest = self._oldest_unconsumed(
                    list(g.loc[g["ВидНорм"] == "аванс", ["Дата", "Сумма"]].itertuples(index=False, name=None)),
                    consumed,
                )
                if oldest is not None:
                    age = (ref_date - oldest).days
                    issues.append(
                        f"старейший незачтенный аванс от {oldest.date()} (возраст {age} дн.); "
                        f"ожидается отгрузка/зачет по авансу от {oldest.date()} на {advance_left:,.2f}"
                    )

            # По-счетная разбивка остатков ОСВ (долг/аванс на разных счетах расчетов)
            osv_break = self._osv_settlement_breakdown(name)
            if osv_break:
                accounts = ", ".join(sorted(osv_break))
                issues.append(
                    "ОСВ по счетам: "
                    + "; ".join(f"{a} {'Д' if n > 0 else 'К'} {abs(n):,.2f}" for a, n in sorted(osv_break.items()))
                )
            else:
                accounts = ", ".join(sorted(set(g["Счет"].astype(str))))

            comment = "; ".join(issues) if issues else "Расчеты закрыты документами"
            if not unknown.empty:
                comment += f"; не распознаны операции: {', '.join(sorted(set(unknown['Вид'])))}"

            osv_net = float(self._osv_settlement_balance().get(name, 0.0))
            if abs(osv_net - open_amount) > 1e-6:
                comment += f"; расхождение с остатком ОСВ ({osv_net:,.2f})"

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
        return self.errors

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
                    "Дебет": r.get("КонецДебет", 0.0),
                    "Кредит": r.get("КонецКредит", 0.0),
                    "Сумма": r.get("Сумма", abs(float(r.get("КонецДебет", 0.0) - r.get("КонецКредит", 0.0)))),
                    "Комментарий": r.get("Комментарий", ""),
                })
        return pd.DataFrame(rows, columns=DETAIL_COLUMNS)

    @staticmethod
    def _account_codes_in(cell: object) -> list[str]:
        """Разбирает значение «Счет» (возможно «51, 62.01») на отдельные коды."""
        return [
            c.strip()
            for c in str(cell).replace(";", ",").split(",")
            if c.strip()
        ]

    def accounts_with_errors(self) -> list[str]:
        """Отсортированный список счетов, по которым есть нарушения."""
        codes: set[str] = set()
        details = self.details_df()
        if not details.empty:
            for cell in details["Счет"].dropna():
                codes.update(self._account_codes_in(cell))
        return sorted(codes)

    def account_report_df(self, account_code: str) -> pd.DataFrame:
        """Все нарушения выбранного счета одним списком (строки детального
        отчета, где в «Счет» присутствует account_code)."""
        details = self.details_df()
        if details.empty:
            return details
        return details[
            details["Счет"].map(
                lambda cell: account_code in self._account_codes_in(cell)
            )
        ]

    def account_subconto(self, account_code: str) -> list[str]:
        """Субконто/контрагенты, задействованные по счету (из ОСВ и документов)."""
        names: set[str] = set()
        if "Счет" in self.balances.columns and "Субконто" in self.balances.columns:
            rows = self.balances[self.balances["Счет"] == account_code]["Субконто"]
            names.update(
                str(n).strip() for n in rows.dropna() if str(n).strip() != "-"
            )
        if (
            self.documents is not None
            and "Счет" in self.documents.columns
            and "Контрагент" in self.documents.columns
        ):
            rows = self.documents[self.documents["Счет"] == account_code]["Контрагент"]
            names.update(
                str(n).strip() for n in rows.dropna() if str(n).strip()
            )
        return sorted(names)

    def account_subconto_duplicates(
        self,
        account_code: str,
        threshold: Optional[int] = None,
    ) -> pd.DataFrame:
        """ML-поиск возможных дублей контрагентов внутри выбранного счета."""
        names = self.account_subconto(account_code)
        if not names:
            return pd.DataFrame(columns=["Субконто", "Название А", "Название Б", "Сходство", "Комментарий"])
        return ml.find_duplicate_counterparties(
            names, threshold=threshold if threshold is not None else self.dup_threshold
        )

    def accounts_summary_df(self) -> pd.DataFrame:
        """Сводка по счетам для листа «По счетам» в экспорте Excel/PDF."""
        columns = ["Счет", "Кол-во нарушений", "Проверки", "Периоды", "Сумма", "Дубли контрагентов"]
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

    def to_excel(self, account_pass: Optional[dict] = None) -> bytes:
        """
        Сводный + детальный отчет с цветовой индикацией (ТЗ п.6, 14).

        :param account_pass: результат автопрохода по счетам (см. core.account_pass)
            — добавляет лист «Проход по счетам».
        """

        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill

        summary = self.summary_df()
        details = self.details_df()
        by_account = self.accounts_summary_df()

        pass_details = pd.DataFrame()
        if account_pass is not None:
            pd_ = account_pass.get("details_df")
            if pd_ is not None and not getattr(pd_, "empty", True):
                pass_details = pd_

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            if self._meta_payload():
                meta_rows = [
                    {"Параметр": "Заголовок", "Значение": self.meta.get("title", "")},
                    {"Параметр": "Организация", "Значение": self.meta.get("organization", "")},
                    {"Параметр": "Период", "Значение": self.meta.get("period", "")},
                    {"Параметр": "Сформировано", "Значение": pd.Timestamp.now().strftime("%d.%m.%Y %H:%M")},
                ]
                pd.DataFrame(meta_rows).to_excel(writer, sheet_name="Об отчете", index=False)

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

            sheets_to_color = [
                ("Сводный отчет", 2),
                ("Детальный отчет", 2),
                ("По счетам", 2),
            ]
            if not pass_details.empty:
                sheets_to_color.append(("Проход по счетам", 2))

            for sheet, level_col in sheets_to_color:
                ws = wb[sheet]
                for cell in ws[1]:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center")
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

        return buf.getvalue()

    # ============ PDF-отчет (ТЗ п.6.2) ============
    @staticmethod
    def _find_pdf_font() -> Optional[str]:
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
    def _pdf_header(pdf, headers: list[str], widths: list[float]) -> None:
        pdf.set_fill_color(68, 114, 196)
        pdf.set_text_color(255, 255, 255)
        for w, h in zip(widths, headers):
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln()
        pdf.set_text_color(0, 0, 0)

    @staticmethod
    def _pdf_row(pdf, values: list[str], widths: list[float]) -> None:
        for w, v in zip(widths, values):
            pdf.cell(w, 6, str(v), border=1)
        pdf.ln()

    def to_pdf(self, account_pass: Optional[dict] = None) -> bytes:
        """
        Печатный отчет в PDF (ТЗ п.6.2): шапка с реквизитами, сводный
        и детальный отчет, рекомендации, отчет по счетам.

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

        pdf = FPDF(format="A4")
        pdf.set_margins(10, 10, 10)
        pdf.add_font("DejaVu", "", font_path)
        pdf.add_page()

        pdf.set_font("DejaVu", size=14)
        pdf.cell(
            0, 10,
            self.meta.get("title") or "Отчет автоматического аудита 1С",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C"
        )
        pdf.ln(2)

        pdf.set_font("DejaVu", size=10)
        for label, value in (
            ("Организация", self.meta.get("organization", "")),
            ("Период", self.meta.get("period", "")),
        ):
            if value:
                pdf.cell(
                    0, 6,
                    f"{label}: {value}",
                    new_x=XPos.LMARGIN, new_y=YPos.NEXT
                )
        pdf.cell(
            0, 6,
            f"Сформировано: {pd.Timestamp.now().strftime('%d.%m.%Y %H:%M')}",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.ln(3)

        report = self.report()
        pdf.set_font("DejaVu", size=12)
        pdf.cell(
            0, 8, f"Статус: {report['status_label']}",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.set_font("DejaVu", size=10)
        pdf.cell(
            0, 6,
            f"Красных флагов: {report['total_flags']}; ",
            new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )
        pdf.ln(3)

        summary = self.summary_df()
        pdf.set_font("DejaVu", size=12)
        pdf.cell(0, 8, "Сводный отчет", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("DejaVu", size=8)
        sum_widths = [58, 22, 16, 38, 56]
        self._pdf_header(pdf, list(summary.columns), sum_widths)
        for _, r in summary.iterrows():
            self._pdf_row(pdf, [
                str(r["Проверка"])[:32],
                str(r["Уровень"]),
                str(r["Строк"]),
                f"{r['Сумма']:,.2f}",
                str(r["Рекомендации"])[:80],
            ], sum_widths)
        pdf.ln(4)

        pdf.set_font("DejaVu", size=12)
        pdf.cell(0, 8, "Рекомендации", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("DejaVu", size=9)
        for _, r in summary.iterrows():
            if not r["Рекомендации"]:
                continue
            pdf.multi_cell(
                0, 5, f"{r['Проверка']}: {r['Рекомендации']}",
                new_x=XPos.LMARGIN, new_y=YPos.NEXT
            )
        pdf.ln(4)

        details = self.details_df()
        pdf.set_font("DejaVu", size=12)
        pdf.cell(0, 8, "Детальный отчет", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("DejaVu", size=7)
        det_widths = [38, 14, 18, 12, 22, 13, 13, 13, 47]
        self._pdf_header(pdf, list(details.columns), det_widths)
        for _, r in details.iterrows():
            self._pdf_row(pdf, [
                str(r["Проверка"])[:20],
                str(r["Уровень"]),
                str(r["Период"])[:12],
                str(r["Счет"])[:8],
                str(r["Субконто"])[:14],
                f"{r['Дебет']:,.0f}",
                f"{r['Кредит']:,.0f}",
                f"{r['Сумма']:,.0f}",
                str(r["Комментарий"])[:45],
            ], det_widths)

        # Отчет по счетам
        accounts = self.accounts_with_errors()
        if accounts:
            pdf.add_page()
            pdf.set_font("DejaVu", size=12)
            pdf.cell(0, 8, "Отчет по счетам", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            acc_widths = [34, 34, 18, 18, 34, 18]
            self._pdf_header(pdf, ["Проверка", "Период", "Субконто", "Дебет", "Кредит", "Сумма"], acc_widths)
            for account in accounts:
                acc_rep = self.account_report_df(account)
                dups = self.account_subconto_duplicates(account)
                pdf.set_font("DejaVu", size=10)
                pdf.cell(
                    0, 7,
                    f"Счет {account} — нарушений: {len(acc_rep)}, "
                    f"дублей контрагентов: {len(dups)}",
                    new_x=XPos.LMARGIN, new_y=YPos.NEXT
                )
                pdf.set_font("DejaVu", size=7)
                for _, r in acc_rep.iterrows():
                    self._pdf_row(pdf, [
                        str(r["Проверка"])[:20],
                        str(r["Период"])[:12],
                        str(r["Субконто"])[:12],
                        f"{r['Дебет']:,.0f}",
                        f"{r['Кредит']:,.0f}",
                        f"{r['Сумма']:,.0f}",
                    ], acc_widths)
                if not dups.empty:
                    pdf.set_font("DejaVu", size=7)
                    for _, d in dups.iterrows():
                        pdf.cell(
                            0, 5,
                            f"  Дубль: {d['Название А']} ≈ {d['Название Б']} "
                            f"({d['Сходство']:.0f}%)",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT
                        )
                pdf.ln(2)

        # Автопроход по счетам (1С:Фреш)
        if account_pass is not None:
            pass_details = account_pass.get("details_df")
            if pass_details is not None and not getattr(pass_details, "empty", True):
                pass_summary = account_pass.get("summary_df")
                pass_dups = account_pass.get("duplicates_df")

                pdf.add_page()
                pdf.set_font("DejaVu", size=12)
                pdf.cell(
                    0, 8, "Автопроход по счетам (1С:Фреш)",
                    new_x=XPos.LMARGIN, new_y=YPos.NEXT
                )
                if pass_summary is not None and not getattr(pass_summary, "empty", True):
                    pdf.set_font("DejaVu", size=8)
                    sum_widths = [40, 30, 22, 22, 34, 40]
                    self._pdf_header(pdf, list(pass_summary.columns), sum_widths)
                    for _, r in pass_summary.iterrows():
                        self._pdf_row(pdf, [
                            str(r["Счет"])[:8],
                            str(r["Строк нарушений"]),
                            str(r["Уровень"])[:10],
                            str(r["Субконто"]),
                            f"{r['Сумма']:,.0f}",
                            str(r["Ошибка"])[:35],
                        ], sum_widths)
                    pdf.ln(3)

                pdf.set_font("DejaVu", size=10)
                for account in sorted(set(str(c) for c in pass_details["Счет"].dropna())):
                    acc_rows = pass_details[pass_details["Счет"].astype(str) == account]
                    dups_n = 0
                    if pass_dups is not None and not pass_dups.empty:
                        dups_n = int((pass_dups["Счет"].astype(str) == account).sum())
                    pdf.cell(
                        0, 7,
                        f"Счет {account} — нарушений: {len(acc_rows)}, "
                        f"дублей контрагентов: {dups_n}",
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT
                    )
                    pdf.set_font("DejaVu", size=7)
                    for _, r in acc_rows.iterrows():
                        self._pdf_row(pdf, [
                            str(r.get("Проверка", ""))[:20],
                            str(r.get("Уровень", "")),
                            str(r.get("Период", ""))[:12],
                            str(r.get("Субконто", ""))[:14],
                            f"{r.get('Дебет', 0.0):,.0f}",
                            f"{r.get('Кредит', 0.0):,.0f}",
                            f"{r.get('Сумма', 0.0):,.0f}",
                        ], [48, 16, 28, 30, 22, 22, 26])
                    pdf.set_font("DejaVu", size=10)
                    pdf.ln(2)

        return bytes(pdf.output())
