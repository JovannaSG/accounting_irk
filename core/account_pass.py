"""
Автоматический проход по счетам с нарушениями (1С:Фреш).

После общего аудита ОСВ для каждого счёта с красными флагами запрашивается
индивидуальный отчёт по счёту (ОСВ в разрезе субконто по месяцам) и в нём
повторно ищутся красные флаги: все включённые проверки + ML-дубли
контрагентов внутри счёта.

Сбой загрузки/аудита одного счёта не останавливает проход: счёт помечается
ошибкой и обработка продолжается.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from core.auditor import DETAIL_COLUMNS, AutoAuditor1C

# Столбцы сводки автопрохода.
PASS_SUMMARY_COLUMNS: list[str] = [
    "Счет", "Строк нарушений", "Уровень",
    "Субконто", "Сумма", "Ошибка",
]

# Столбцы сводки дублей контрагентов в автопроходе (со «Счет» в начале)
PASS_DUPLICATES_COLUMNS: list[str] = [
    "Счет", "Субконто", "Название А",
    "Название Б", "Сходство", "Комментарий",
]


def _build_auditor(
    balances: pd.DataFrame,
    options: dict,
    meta: dict | None = None,
) -> AutoAuditor1C:
    """
    Собирает аудитор с теми же настройками, что и общий аудит
    """

    return AutoAuditor1C(
        balances,
        None,  # реестр документов для 1С:Фреш недоступен через OData
        closing_accounts=options.get("closing_accounts"),
        checks=set(options.get("checks") or []),
        meta=meta or {},
        balance_group_checks=options.get("balance_group_checks", False),
        ml_enabled=options.get("ml_enabled", False),
        ml_amount_anomalies=options.get("ml_amount_anomalies", True),
        ml_turnover_jumps=options.get("ml_turnover_jumps", True),
        ml_duplicates=options.get("ml_duplicates", True),
        dup_threshold=options.get("dup_threshold", 90),
        anomaly_min_abs=options.get("anomaly_min_abs", 1000.0),
    )


def _worst_level(details: pd.DataFrame) -> str:
    """
    Уровень по максимальной тяжести строк нарушений (error > warning > ok)
    """

    if details is None or details.empty:
        return "ok"

    levels = set(str(l) for l in details["Уровень"].dropna())
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


def run_account_pass(
    accounts: list[str],
    fetch_balances: Callable[[str], pd.DataFrame],
    options: dict,
    meta: dict | None = None,
    progress: Callable[[int, int, str]] | None = None,
) -> dict:
    """
    Автопроход по счетам: для каждого счёта берёт индивидуальный отчёт
    (fetch_balances), запускает включённые проверки и собирает результат.

    :param accounts: счета с нарушениями из общего аудита
        (auditor.accounts_with_errors()).
    :param fetch_balances: callable(account_code) -> DataFrame со схемой OSV_COLUMNS.
    :param options: настройки аудита (checks, closing_accounts, ml_enabled, ...).
    :param meta: реквизиты отчёта (организация/период), передаются в аудитор.
    :param progress: callable(done, total, message) перед обработкой счёта.

    Возвращает dict:
      summary_df    — сводка по счетам (PASS_SUMMARY_COLUMNS);
      details_df    — нарушения по всем счетам (DETAIL_COLUMNS);
      duplicates_df — ML-дубли контрагентов по счетам (PASS_DUPLICATES_COLUMNS);
      by_account    — {code: {balances, details, subconto, dups, error}}.
    """

    ordered = sorted({str(a) for a in accounts})
    total = len(ordered)

    summary_rows: list[dict] = []
    all_details: list[pd.DataFrame] = []
    all_dups: list[pd.DataFrame] = []
    by_account: dict[str, dict] = {}

    for i, account in enumerate(ordered, start=1):
        if progress is not None:
            progress(i, total, account)

        record: dict = {
            "account": account,
            "balances": pd.DataFrame(),
            "details": pd.DataFrame(columns=DETAIL_COLUMNS),
            "subconto": [],
            "dups": pd.DataFrame(),
            "error": None,
        }
        try:
            balances = fetch_balances(account)
            if balances is None or getattr(balances, "empty", True):
                raise ValueError(
                    "По счёту нет данных в отчёте 1С (нет строк регистра за период)"
                )
            record["balances"] = balances

            auditor = _build_auditor(balances, options, meta)
            auditor.run_audit()
            details = auditor.details_df()
            # Все строки отчёта принадлежат обрабатываемому счёту (в т.ч. ML-дубли,
            # у которых нет колонки «Счет») — проставляем счёт явно.
            if not details.empty:
                details = details.copy()
                details["Счет"] = account
            record["details"] = details
            record["subconto"] = auditor.account_subconto(account)
            record["dups"] = auditor.account_subconto_duplicates(account)
        except Exception as exc:  # noqa: BLE001 - одиночный счёт не должен рушить проход
            record["error"] = str(exc)

        details = record["details"]
        level = "error" if record["error"] else _worst_level(details)

        if not details.empty:
            all_details.append(details)
        dups = record["dups"]
        if dups is not None and not dups.empty:
            dups = dups.copy()
            dups["Счет"] = account
            dups = dups.reindex(columns=PASS_DUPLICATES_COLUMNS)
            all_dups.append(dups)

        summary_rows.append({
            "Счет": account,
            "Строк нарушений": len(details),
            "Уровень": level,
            "Субконто": len(record["subconto"]),
            "Сумма": round(float(details["Сумма"].sum()), 2) if not details.empty else 0.0,
            "Ошибка": record["error"] or "",
        })
        by_account[account] = record

    return {
        "summary_df": pd.DataFrame(summary_rows, columns=PASS_SUMMARY_COLUMNS),
        "details_df": (
            pd.concat(all_details, ignore_index=True)
            if all_details else pd.DataFrame(columns=DETAIL_COLUMNS)
        ),
        "duplicates_df": (
            pd.concat(all_dups, ignore_index=True)
            if all_dups else pd.DataFrame(columns=PASS_DUPLICATES_COLUMNS)
        ),
        "by_account": by_account,
    }
