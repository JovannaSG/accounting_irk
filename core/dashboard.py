"""
Сводный дашборд по базам (Master-Detail).

Мастер-вид — таблица: по одной строке на базу, в колонках — счета (и периоды)
с нарушениями по типу проверки. Детальный вид строится в app/ui.py из
результата выбранной базы.

Вся циклическая логика обработки данных использует исключительно циклы while.
"""

from __future__ import annotations

import pandas as pd

# Столбцы мастер-таблицы (строго по образцу).
DASHBOARD_COLUMNS: list[str] = [
    "Бухгалтер",
    "База",
    "Дата просмотра",
    "Сальдо красным, счет",
    "Развернутое сальдо, счет",
    "Не закрыт период, счет, период",
    "Не закрыты документами, счет",
]

_DASH: str = "—"  # пустая ячейка вместо пустого списка

# Три блока детальной панели дашборда: маркеры заголовков проверок.
BLOCK_RULES: dict[str, list[str]] = {
    "red": ["Красное сальдо"],
    "unclosed": ["Незакрытое сальдо", "Зависшее сальдо", "Контроль групп"],
    "expanded": ["Развернутое сальдо"],
}


def _block_columns() -> list[str]:
    """
    Порядок блоков детальной панели
    """

    return list(BLOCK_RULES.keys())


def block_dfs(details: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Срезы details_df по трём блокам (красное сальдо / незакрытые / развёрнутое).

    Строки ML-проверок и «расчеты не закрыты документами» (4.5) в блоки не
    попадают — они остаются в общей детализации по счетам.
    """

    result: dict[str, pd.DataFrame] = {}
    blocks = _block_columns()
    if details is None or details.empty or "Проверка" not in details.columns:
        idx = 0
        while idx < len(blocks):
            result[blocks[idx]] = pd.DataFrame()
            idx += 1
        return result

    idx: int = 0
    while idx < len(blocks):
        block = blocks[idx]
        markers = BLOCK_RULES[block]
        mask = pd.Series(False, index=details.index)
        m_idx = 0
        while m_idx < len(markers):
            mask = mask | details["Проверка"].astype(str).str.contains(markers[m_idx], na=False)
            m_idx += 1
        result[block] = details[mask].copy()
        idx += 1
    return result


def accounts_list(df: pd.DataFrame | None) -> list[str]:
    """
    Сортированные уникальные счета из колонки «Счет»
    """

    if df is None or df.empty or "Счет" not in df.columns:
        return []

    values = df["Счет"].dropna().astype(str)
    seen: set[str] = set()
    idx = 0
    while idx < len(values):
        item = str(values.iloc[idx]).strip()
        if item:
            seen.add(item)
        idx += 1
    return sorted(seen)


def _collect_accounts(details: pd.DataFrame) -> dict[str, list[str]]:
    """
    Разбивает строки нарушений по типам проверок.

    Возвращает {колонка: [значения]}:
      "Сальдо красным, счет"          -> счета (красное сальдо);
      "Развернутое сальдо, счет"      -> счета (развернутое сальдо);
      "Не закрыт период, счет, период"-> "счет, период";
      "Не закрыты документами, счет"  -> счета (расчеты не закрыты документами).
    """

    result: dict[str, list[str]] = {}
    col_idx = 0
    while col_idx < len(DASHBOARD_COLUMNS[3:]):
        result[DASHBOARD_COLUMNS[3:][col_idx]] = []
        col_idx += 1

    rows = details.to_dict("records")
    idx = 0
    while idx < len(rows):
        row = rows[idx]
        title = str(row.get("Проверка") or "")
        acc = str(row.get("Счет") or "").strip()
        period = str(row.get("Период") or "").strip()
        if acc:
            if "Красное сальдо" in title:
                result["Сальдо красным, счет"].append(acc)
            elif "Развернутое сальдо" in title:
                result["Развернутое сальдо, счет"].append(acc)
            elif "документами" in title or "расчеты" in title:
                result["Не закрыты документами, счет"].append(acc)
            elif (
                "Незакрытое сальдо" in title
                or "Зависшее сальдо" in title
                or "Контроль групп" in title
            ):
                value = f"{acc}, {period}" if period else acc
                result["Не закрыт период, счет, период"].append(value)
        idx += 1
    return result


def build_master_row(result: dict) -> dict:
    """
    Сводная строка одной базы для мастер-таблицы дашборда.
    """

    details = result.get("details")
    collected: dict[str, list[str]] = {}
    if details is not None and not getattr(details, "empty", True):
        collected = _collect_accounts(details)

    row: dict[str, str] = {
        "Бухгалтер": str(result.get("accountant") or "") or _DASH,
        "База": str(result.get("db_name") or "") or _DASH,
        "Дата просмотра": str(result.get("viewed_at") or "") or _DASH,
    }

    # При аудите «по месяцам» каждая строка истории соответствует отдельному
    # периоду одной базы — чтобы строки в мастер-таблице не выглядели клонами,
    # период добавляется к имени базы.
    period = str(result.get("period") or "").strip()
    if period and period not in row["База"]:
        row["База"] = f"{row['База']} ({period})"
    col_idx = 0
    while col_idx < len(DASHBOARD_COLUMNS[3:]):
        col = DASHBOARD_COLUMNS[3:][col_idx]
        values = sorted(set(collected.get(col, [])))
        row[col] = ", ".join(values) if values else _DASH
        col_idx += 1
    return row


def build_dashboard_df(history: list[dict]) -> pd.DataFrame:
    """
    Мастер-таблица дашборда: одна строка на результат аудита в истории.
    """

    rows: list[dict] = []
    idx = 0
    while idx < len(history):
        rows.append(build_master_row(history[idx]))
        idx += 1
    if not rows:
        return pd.DataFrame(columns=DASHBOARD_COLUMNS)

    return pd.DataFrame(rows, columns=DASHBOARD_COLUMNS)


def find_result(history: list[dict], db_name: str) -> dict | None:
    """
    Первый результат аудита для указанной базы (учитывает период в имени)
    """

    idx: int = 0
    while idx < len(history):
        entry = history[idx]
        base = str(entry.get("db_name") or "")
        period = str(entry.get("period") or "").strip()
        candidates = {base}
        if period:
            candidates.add(f"{base} ({period})")
        if db_name in candidates:
            return entry
        idx += 1

    return None
