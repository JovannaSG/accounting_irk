"""
ML-проверки: статистический поиск аномалий и нечёткий поиск дублей.

Подход — unsupervised и детерминированный (без обучения моделей):

  - нетипичные суммы операций по контрагенту (медиана + MAD);
  - резкие скачки оборотов между периодами по счету/аналитике;
  - нечёткий поиск дублей контрагентов (rapidfuzz; при отсутствии
    библиотеки — фолбэк на difflib из стандартной библиотеки).

Все функции возвращают pandas.DataFrame находок (пустой при отсутствии),
чтобы вызывающий код мог единообразно встроить их в отчёт.
"""

from __future__ import annotations

import warnings
from difflib import SequenceMatcher
from typing import Iterable, Any

import pandas as pd
import numpy as np  # Импортирован для оптимизации матрицы

_fuzz: Any = None
_process: Any = None
try:
    from rapidfuzz import fuzz as _fuzz
    from rapidfuzz import process as _process

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - запасной путь без зависимостей
    _HAVE_RAPIDFUZZ = False

# Параметры по умолчанию (переопределяются в конструкторе AutoAuditor1C / UI)
DEFAULT_K: float = 8.0               # множитель MAD для аномалий сумм
DEFAULT_MIN_ABS: float = 1_000.0     # минимальная аномальная сумма
DEFAULT_MIN_OPS: int = 3             # минимум операций по контрагенту для статистики
DEFAULT_JUMP_RATIO: float = 20.0     # во сколько раз могут вырасти обороты между периодами
DEFAULT_JUMP_MIN_ABS: float = 1_000_000.0
DEFAULT_SIM_THRESHOLD: int = 90      # порог сходства для дублей (0..100)
MAX_NAMES: int = 2000                # лимит имен против O(n^2)

ANOMALY_COLUMNS: list[str] = [
    "Дата", "Документ", "Контрагент", "Вид",
    "Сумма", "Медиана", "Отклонение", "Комментарий",
]
JUMP_COLUMNS: list[str] = [
    "Период", "Счет", "Субконто", "ОборотДебет",
    "ОборотКредит", "Отношение", "Сумма", "Комментарий",
]
DUP_COLUMNS: list[str] = [
    "Субконто", "Название А", "Название Б", "Сходство", "Комментарий",
]

_JUMP_FROM_ZERO = 999.0  # показываемая «кратность» при росте оборотов с нуля


def _require(df: pd.DataFrame, columns: list[str], what: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{what}: отсутствуют колонки: {', '.join(missing)}")


#              ========== 1. Нетипичные суммы операций по контрагенту ==========
def detect_amount_anomalies(
    documents_df: pd.DataFrame,
    k: float = DEFAULT_K,
    min_abs: float = DEFAULT_MIN_ABS,
    min_ops: int = DEFAULT_MIN_OPS,
) -> pd.DataFrame:
    """
    Ищет операции, чья сумма статистически выделяется по истории контрагента.

    Для каждого контрагента (не менее min_ops операций) считаются медиана и MAD
    от |Сумма|. Операция считается аномальной, если |Сумма| превышает
    max(медиана + k * MAD, 10 * медиана, min_abs).
    """

    _require(documents_df, ["Контрагент", "Сумма"], "detect_amount_anomalies")

    rows: list[dict] = []
    for name, g in documents_df.groupby("Контрагент"):
        if len(g) < min_ops:
            continue

        vals = g["Сумма"].abs()
        med = float(vals.median())
        mad = float((vals - med).abs().median())
        if med <= 0:
            continue

        limit = max(med + k * mad, 10 * med, min_abs)
        for _, r in g.iterrows():
            v = abs(float(r["Сумма"]))
            if v > limit:
                # Оптимизация: med гарантированно больше нуля благодаря проверке выше
                ratio = v / med
                rows.append({
                    "Дата": r.get("Дата", ""),
                    "Документ": r.get("Документ", ""),
                    "Контрагент": name,
                    "Вид": r.get("Вид", ""),
                    "Сумма": v,
                    "Медиана": med,
                    "Отклонение": round(ratio, 1),
                    "Комментарий": (
                        f"Сумма {v:,.2f} в {ratio:.1f} раз превышает "
                        f"медиану {med:,.2f} по контрагенту"
                    ),
                })

    return pd.DataFrame(rows, columns=ANOMALY_COLUMNS)


#                ========== 2. Резкие скачки оборотов между периодами ==========
def detect_turnover_jumps(
    balances_df: pd.DataFrame,
    ratio: float = DEFAULT_JUMP_RATIO,
    min_abs: float = DEFAULT_JUMP_MIN_ABS,
) -> pd.DataFrame:
    """
    Ищет скачки оборотов (Д и К по отдельности) между соседними периодами
    по счету/аналитике. Требуются данные минимум за 2 периода; при одном
    периоде проверка просто не находит ничего.
    """

    _require(
        balances_df,
        [
            "Период", "Счет", "Субконто",
            "ОборотДебет", "ОборотКредит"
        ],
        "detect_turnover_jumps"
    )

    b = balances_df.sort_values(["Счет", "Субконто", "Период"])
    rows: list[dict] = []
    for (code, sub), g in b.groupby(["Счет", "Субконто"]):
        if len(g) < 2:
            continue

        for i in range(1, len(g)):
            prev, cur = g.iloc[i - 1], g.iloc[i]
            for col in ("ОборотДебет", "ОборотКредит"):
                p, c = float(prev[col]), float(cur[col])
                if c == 0 and p == 0:
                    continue
                if abs(c - p) < min_abs:
                    continue
                if p == 0 and c != 0:
                    jump = _JUMP_FROM_ZERO
                elif c == 0:
                    jump = 0.0
                else:
                    jump = c / p
                # Скачок — и рост (>= ratio), и падение (<= 1/ratio) оборотов.
                if 1 / ratio < jump < ratio:
                    continue
                rows.append({
                    "Период": cur["Период"],
                    "Счет": code,
                    "Субконто": sub,
                    "ОборотДебет": float(cur["ОборотДебет"]),
                    "ОборотКредит": float(cur["ОборотКредит"]),
                    "Отношение": jump,
                    "Сумма": abs(c - p),
                    "Комментарий": (
                        f"{col} изменились с {p:,.2f} до {c:,.2f} "
                        f"(в {jump:.1f} раз) между периодами"
                    ),
                })

    return pd.DataFrame(rows, columns=JUMP_COLUMNS)


#                     ========== 3. Нечёткий поиск дублей контрагентов ==========
def _normalize_name(name: object) -> str:
    """
    Приводит название к сравниваемой форме: только буквы/цифры и пробелы
    """

    s = str(name).strip().lower()
    chars = [c for c in s if c.isalnum() or c == " "]
    return " ".join("".join(chars).split())


def _token_sort_ratio(a: str, b: str) -> float:
    """
    Сходство с неважным порядком слов (фолбэк на difflib).

    Реализует идею token_sort_ratio из rapidfuzz: слова сортируются
    по алфавиту и сравнивается итоговая последовательность.
    """

    if not a or not b:
        return 0.0

    sorted_a = " ".join(sorted(a.split()))
    sorted_b = " ".join(sorted(b.split()))
    if sorted_a == sorted_b:
        return 100.0

    return SequenceMatcher(None, sorted_a, sorted_b).ratio() * 100.0


def _duplicate_pairs(
    names: list[str],
    norm: list[str],
    threshold: int,
) -> list[tuple[str, str, float]]:
    """
    Возвращает пары (имя А, имя Б, сходство) со сходством >= threshold
    """

    pairs: list[tuple[str, str, float]] = []
    n = len(names)
    if n < 2:
        return pairs

    if _HAVE_RAPIDFUZZ:
        matrix = _process.cdist(norm, norm, scorer=_fuzz.token_sort_ratio)
        # Используем numpy для векторного извлечения
        # индексов из верхнего треугольника
        indices = np.argwhere(np.triu(matrix >= threshold, k=1))

        for i, j in indices:
            score = float(matrix[i, j])
            pairs.append((names[i], names[j], round(score, 1)))
    else:  # pragma: no cover - запасной путь без rapidfuzz
        for i in range(n):
            for j in range(i + 1, n):
                score = _token_sort_ratio(norm[i], norm[j])
                if score >= threshold:
                    pairs.append((names[i], names[j], round(score, 1)))
    return pairs


def find_duplicate_counterparties(
    names: Iterable[object],
    threshold: int = DEFAULT_SIM_THRESHOLD,
    max_names: int = MAX_NAMES,
) -> pd.DataFrame:
    """
    Находит пары похожих названий («ООО Ромашка» vs «Ромашка, ООО»).

    Вход — произвольная последовательность названий (Субконто из ОСВ,
    Контрагент из реестра документов). Сравнение с неважным порядком слов;
    регистр и пунктуация игнорируются.
    """

    seen: dict[str, str] = {}
    for name in names:
        s = str(name).strip()
        if not s or s == "-":
            continue
        key = _normalize_name(s)
        if key and key not in seen:
            seen[key] = s

    unique_names = list(seen.values())
    unique_norm = list(seen.keys())
    if len(unique_names) > max_names:
        warnings.warn(
            f"Дублей контрагентов: уникальных названий {len(unique_names)} — "
            f"обработаны только первые {max_names}",
            stacklevel=2,
        )
        unique_names = unique_names[:max_names]
        unique_norm = unique_norm[:max_names]

    pairs = _duplicate_pairs(unique_names, unique_norm, threshold)
    rows = [{
        "Субконто": f"{a} ≈ {b}",
        "Название А": a,
        "Название Б": b,
        "Сходство": score,
        "Комментарий": "Возможный дубль контрагента в базе",
    } for a, b, score in pairs]

    return pd.DataFrame(rows, columns=DUP_COLUMNS)
