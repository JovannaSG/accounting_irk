"""
TODO: needs review
Загрузка и нормализация ОСВ из файлов, выгруженных из 1С.

Поддерживаются форматы:
  - XLS/XLSX (табличный документ 1С, «Сохранить как» → Excel)
  - HTML (табличный документ 1С, «Сохранить как» → HTML)
  - CSV (плоская таблица с колонками-алиасами)

MXL не поддерживается: для этого проприетарного бинарного формата нет
надежных Python-парсеров — в 1С нужно сохранить отчет как Excel/HTML.
"""

from __future__ import annotations

import io
import numbers
import re
import warnings
from html.parser import HTMLParser
from typing import Optional

import pandas as pd

from core.auditor import account_group, normalize_balances

ACCOUNT_CODE_RE = re.compile(r"^[0-9]+(?:\.[A-Za-zА-Яа-я0-9]+)*$")
TOTAL_KEYWORDS: tuple = ("total", "итого", "всего", "итог", "итого по")

# Значения колонки «Показатели» в ОСВ, выведенной по БУ/НУ/БУ-НУ.
# Из такой ведомости аудит использует только строки БУ; НУ/БУ-НУ и Вал.
# (валютные суммы) игнорируются.
INDICATOR_LABELS: tuple = ("БУ", "НУ", "БУ-НУ", "Вал.")

# Тип по группам счетов типового плана счетов 1С:Бухгалтерия 8 (ред. 3.0).
# Источник: план счетов конфигурации (колонка «Вид»); счета 16, 40, 60, 62,
# 68, 69, 70, 71, 73, 75, 76, 79, 84, 90, 91, 96, 99 — активно-пассивные.
# Используется как приоритетный источник при определении Тип счета;
# эвристика по остаткам применяется для счетов, отсутствующих в плане.
PLAN_OF_ACCOUNTS: dict = {
    "01": "A", "02": "P", "03": "A", "04": "A", "05": "P", "07": "A", "08": "A",
    "09": "A", "10": "A", "11": "A", "14": "P", "15": "A", "16": "AP", "19": "A",
    "20": "A", "21": "A", "23": "A", "25": "A", "26": "A", "28": "A", "29": "A",
    "40": "AP", "41": "A", "42": "P", "43": "A", "44": "A", "45": "A", "46": "A",
    "50": "A", "51": "A", "52": "A", "55": "A", "57": "A", "58": "A", "59": "P",
    "60": "AP", "62": "AP", "63": "P", "66": "P", "67": "P", "68": "AP",
    "69": "AP", "70": "AP", "71": "AP", "73": "AP", "75": "AP", "76": "AP",
    "77": "P", "79": "AP", "80": "P", "81": "P", "82": "P", "83": "P",
    "84": "AP", "86": "P", "90": "AP", "91": "AP", "94": "A", "96": "AP",
    "97": "A", "98": "P", "99": "AP", "000": "AP",
}


def detect_format(filename: str, data: bytes) -> str:
    """Определяет формат файла по расширению и содержимому."""
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    head = data[:1024]

    def looks_html() -> bool:
        s = head.lstrip().lower()
        return (s.startswith(b"<html") or s.startswith(b"<!doctype")
                or b"<table" in s.lower() or s.startswith(b"<?xml"))

    if ext == "mxl":
        return "mxl"
    if ext in ("html", "htm") or (ext == "xls" and looks_html()):
        return "html"
    if ext == "csv":
        return "csv"
    if ext == "xlsx" or head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if ext == "xls" or head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if looks_html():
        return "html"
    return "unknown"


def _cell(row, i: int) -> str:
    if i >= len(row):
        return ""
    v = row[i]
    if v is None:
        return ""
    if isinstance(v, float):
        if pd.isna(v):
            return ""
        return str(int(v)) if v == int(v) else str(v)
    if isinstance(v, numbers.Number):
        return str(v)
    return str(v).strip()


def _to_number(v) -> float:
    """Преобразует значение ячейки в число, устойчиво к разделителям разрядов."""
    if v is None:
        return 0.0
    if isinstance(v, numbers.Number) and not isinstance(v, bool):
        return 0.0 if pd.isna(v) else float(v)
    s = str(v).strip().replace("\u00a0", "").replace(" ", "")
    if not s or s in ("-", "—", "–", "−"):
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        warnings.warn(
            f"Нечисловое значение в ячейке ОСВ: {v!r} — заменено на 0",
            stacklevel=2,
        )
        return 0.0


def _parse_plan(text: Optional[str]) -> dict:
    """Разбор пользовательского плана счетов вида '51:A, 60.01:AP'."""
    if not text:
        return {}
    plan = {}
    for chunk in re.split(r"[,;\n]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            continue
        code, _, typ = chunk.partition(":")
        code, typ = code.strip(), typ.strip().upper()
        if typ in ("A", "P", "AP"):
            plan[code] = typ
    return plan


def _extract_period(title: str) -> str:
    m = re.search(r"(?:за|За)\s+(.+)$", title)
    return m.group(1).strip() if m else ""


def _read_excel_grid(data: bytes, engine: str) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(data), header=None, engine=engine)


def _decode_html(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "windows-1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку HTML-файла")


class _TableGridParser(HTMLParser):
    """Извлекает первую (самую большую) таблицу в виде сетки сырого текста ячеек."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[object]]] = []
        self._grid = None
        self._row = None
        self._cell = None
        self._colspan = 1
        self._rowspan = 1
        self._depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: N802
        tag = tag.lower()
        if tag == "table":
            if self._grid is None:
                self._grid = []
            else:
                self._depth += 1
        elif tag == "tr":
            if self._grid is not None and self._depth == 0:
                self._row = []
                self._grid.append(self._row)
        elif tag in ("td", "th"):
            if self._grid is not None and self._depth == 0 and self._row is not None:
                a = dict(attrs)
                self._cell = []
                self._colspan = int(a.get("colspan", 1) or 1)
                self._rowspan = int(a.get("rowspan", 1) or 1)
                self._row.append(self._cell)
        elif tag in ("br", "p"):
            if self._cell is not None:
                self._cell.append(" ")

    def handle_startendtag(self, tag, attrs):  # noqa: N802
        if tag.lower() == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):  # noqa: N802
        tag = tag.lower()
        if tag in ("td", "th"):
            if self._cell is not None:
                text = "".join(self._cell).strip()
                self._row[-1] = (text, self._colspan, self._rowspan)
                self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "table":
            if self._grid is not None:
                if self._depth == 0:
                    self.tables.append(self._grid)
                    self._grid = None
                else:
                    self._depth -= 1


def _expand_spans(raw: list[list[object]]) -> list[list[object]]:
    """Разворачивает colspan/rowspan в прямоугольную сетку."""
    expanded = []
    pending = {}
    for raw_row in raw:
        out = []
        col = 0
        cells = list(raw_row)
        while cells or any(p_col >= col for p_col in pending):
            if col in pending and pending[col][0] > 0:
                rem, text = pending[col]
                pending[col] = (rem - 1, text)
                if pending[col][0] == 0:
                    del pending[col]
                out.append(text)
                col += 1
                continue
            if cells:
                text, cs, rs = cells.pop(0)
                for _ in range(cs):
                    out.append(text)
                if rs > 1:
                    pending[col] = (rs - 1, text)
                col += cs
            else:
                out.append(None)
                col += 1
        expanded.append(out)
    return expanded


def _html_to_grid(data: bytes) -> tuple[pd.DataFrame, str]:
    html = _decode_html(data)
    parser = _TableGridParser()
    parser.feed(html)
    if not parser.tables:
        raise ValueError("В HTML-файле не найдена таблица ОСВ")
    table = max(parser.tables, key=lambda t: sum(len(r) for r in t))
    rows = _expand_spans(table)
    width = max((len(r) for r in rows), default=0)
    grid = pd.DataFrame([r + [None] * (width - len(r)) for r in rows])
    return grid, html


def _looks_like_account(v) -> bool:
    return bool(v) and bool(ACCOUNT_CODE_RE.match(v))


def _has_indicator_column(rows: list, start: int) -> bool:
    """Определяет, выведена ли ОСВ с колонкой «Показатели» (БУ/НУ/БУ-НУ).

    Такая ведомость на одну колонку шире обычной (данные начинаются с 3-й
    колонки). Детекция по заголовку «Показатели» или по значениям строк.
    """
    for row in rows[:start + 1]:
        if str(_cell(row, 0)).strip().lower() == "счет":
            if str(_cell(row, 2)).strip() == "Показатели":
                return True
    for row in rows[start:start + 5]:
        v = row[2] if len(row) > 2 else None
        if isinstance(v, str) and v.strip() in INDICATOR_LABELS:
            return True
    return False


def extract_osv(grid: pd.DataFrame,
                plan_override: Optional[str] = None,
                period_hint: str = "") -> tuple[pd.DataFrame, dict]:
    """Извлекает каноническую ОСВ из «сырой» таблицы 1С (многострочный заголовок)."""
    rows = grid.values.tolist()
    plan = {**PLAN_OF_ACCOUNTS, **_parse_plan(plan_override)}

    title, period, organization = "", period_hint or "", ""
    for row in rows[:6]:
        c0 = _cell(row, 0)
        low = c0.lower()
        if not title and ("ведомость" in low or "оборотно-сальдовая" in low):
            title = c0
            period = period or _extract_period(title)
        if not organization and "организация" in low:
            organization = c0.split(":", 1)[-1].strip() if ":" in c0 else c0

    start = None
    for i, row in enumerate(rows):
        if _looks_like_account(_cell(row, 0)):
            start = i
            break
    if start is None:
        raise ValueError("Не удалось найти строки счетов в таблице ОСВ")

    indicator_mode = _has_indicator_column(rows, start)
    num_range = range(3, 9) if indicator_mode else range(2, 8)

    recs = []
    current_code = None
    for row in rows[start:]:
        c0, c1 = _cell(row, 0), _cell(row, 1)
        if not c0 and not c1:
            continue
        low = c0.lower()
        if low.startswith(TOTAL_KEYWORDS):
            continue
        if indicator_mode:
            ind = row[2] if len(row) > 2 else None
            if not (isinstance(ind, str) and ind.strip() == "БУ"):
                continue
        if _looks_like_account(c0):
            current_code = c0
            subconto = "-"
        else:
            if current_code is None:
                continue
            subconto = c0
        nums = [_to_number(row[c]) for c in num_range]
        recs.append({
            "Период": period,
            "Счет": current_code,
            "Субконто": subconto,
            "НачалоДебет": nums[0], "НачалоКредит": nums[1],
            "ОборотДебет": nums[2], "ОборотКредит": nums[3],
            "КонецДебет": nums[4], "КонецКредит": nums[5],
        })

    if not recs:
        raise ValueError("Не найдено ни одной записи счета в ОСВ")

    types = {}
    by_code: dict = {}
    for r in recs:
        by_code.setdefault(r["Счет"], []).append(r)
    for code, code_recs in by_code.items():
        types[code] = _infer_type(code, plan, code_recs)
    for r in recs:
        r["Тип"] = types[r["Счет"]]

    return pd.DataFrame(recs), {"title": title, "period": period, "organization": organization}


def _infer_type(code: str, plan: dict, recs: list[dict]) -> str:
    if code in plan:
        return plan[code]
    group = account_group(code)
    if group in plan:
        return plan[group]
    has_d = any(abs(r["НачалоДебет"]) > 1e-9 or abs(r["КонецДебет"]) > 1e-9 for r in recs)
    has_k = any(abs(r["НачалоКредит"]) > 1e-9 or abs(r["КонецКредит"]) > 1e-9 for r in recs)
    if has_d and has_k:
        return "AP"
    if has_d:
        return "A"
    if has_k:
        return "P"
    return "AP"


def load_osv_file(filename: str, data: bytes,
                  plan_override: Optional[str] = None) -> tuple[pd.DataFrame, dict]:
    """Загружает ОСВ из файла любого поддерживаемого формата."""
    fmt = detect_format(filename, data)

    if fmt == "mxl":
        raise ValueError(
            "Формат MXL не поддерживается (проприетарный бинарный формат 1С). "
            "Откройте отчет в 1С и сохраните как Excel (xls/xlsx) или HTML."
        )

    if fmt in ("csv",):
        csv = _decode_csv(data)
        return normalize_balances(csv), {"title": "", "period": "", "organization": ""}

    if fmt in ("xls", "xlsx"):
        engine = "xlrd" if fmt == "xls" else "openpyxl"
        grid = _read_excel_grid(data, engine)
        period_hint = ""
    elif fmt == "html":
        grid, html = _html_to_grid(data)
        period_hint = _period_from_html(html)
    else:
        raise ValueError(
            "Не удалось распознать формат файла. Поддерживаются: CSV, XLS, XLSX, HTML."
        )

    df, info = extract_osv(grid, plan_override=plan_override, period_hint=period_hint)
    return normalize_balances(df), info


def _decode_csv(data: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку CSV-файла")


def _period_from_html(html: str) -> str:
    m = re.search(r"Оборотно-сальдовая[^<\n]*?(?:за|За)\s+([^<\n]+)", html)
    return m.group(1).strip() if m else ""
