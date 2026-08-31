"""
NLP-проверка: рискованные формулировки в назначениях платежей (115-ФЗ).

Подход — rule-based: кураторский набор регулярных выражений по категориям
риска (обнал, нетиповые переводы, расплывчатые основания и т.п.).
Проверка активируется только если в реестре документов есть колонка
«Назначение» — при её отсутствии возвращается пустой результат.

Все функции возвращают pandas.DataFrame находок (пустой при отсутствии),
чтобы вызывающий код мог единообразно встроить их в отчёт.
"""

from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

NLP_COLUMNS: list[str] = [
    "Дата", "Документ", "Контрагент", "Вид",
    "Сумма", "Комментарий",
]

# Кураторские правила: (категория, регулярное выражение, описание маркера)
# Текст проверяется в нижнем регистре; шаблоны пишутся только строчными
RISK_PATTERNS: list[tuple[str, str, str]] = [
    (
        "Обнал",
        r"обнал|обналич|кэш",
        "маркер обналичивания",
    ),
    (
        "Комиссии и переводы",
        r"комиссия за перевод|перевод собственных средств"
        r"|перевод денежных средств по реквизитам третьего лица",
        "нетиповой перевод/комиссия",
    ),
    (
        "Пожертвования",
        r"благотворительн|пожертвован|спонсорск",
        "пожертвование/благотворительность между юрлицами",
    ),
    (
        "Займы без договора",
        # (?!...) от начала строки гарантирует,
        # что "договор" и "№" не встретятся НИГДЕ
        r"^(?!.*\bдоговор\b)(?!.*№).*\b(?:за[её]м|займ)\w*",
        "займ без ссылки на договор",
    ),
    (
        "Расплывчатое назначение",
        # Гарантирует, что во всей строке нет ни одной цифры (\d) и знака №
        r"^(?!.*\d)(?!.*№).*\b(?:за\s+(?:услуг|товар|работ)|оплат[аы]\s+по\s+счету)",
        "основание платежа без ссылки на документ",
    ),
    (
        "Третьи лица / прочее",
        r"за третьих лиц|по просьбе|ошибочно перечислен"
        r"|прочие расходы|финансовая помощь|целевое финансирование",
        "нетиповая формулировка",
    ),
    (
        "Наличные",
        r"за наличный расчет|наличными средствами|из кассы",
        "наличная форма расчетов",
    ),
]

# Категория для пользовательских ключевых слов (extra_keywords)
CUSTOM_CATEGORY: str = "Пользовательский маркер"

# максимальная длина цитаты назначения в комментарии
_MAX_SNIPPET: int = 100


def _normalize_text(value: object) -> str:
    """
    Приводит текст назначения к сравнимому виду: нижний регистр, пробелы
    """

    s = str(value).strip().lower()
    return re.sub(r"\s+", " ", s)


def _snippet(text: str, limit: int = _MAX_SNIPPET) -> str:
    """
    Обрезает длинный текст назначения до limit символов с многоточием
    """

    return text if len(text) <= limit else text[:limit - 3] + "..."


def detect_payment_risks(
    documents_df: pd.DataFrame,
    extra_keywords: Iterable[str] | None = None,
) -> pd.DataFrame:
    """
    Ищет в колонке «Назначение» реестра документов маркеры риска (115-ФЗ).

    Для каждого документа проверяются кураторские правила RISK_PATTERNS;
    дополнительно можно передать свои ключевые слова (plain substrings).
    Если колонки «Назначение» нет — возвращается пустой DataFrame
    (проверка тихо пропускается). Одна строка результата — одно попадание
    категории в документ; категория указана в начале комментария.

    :param documents_df: реестр документов (после normalize_documents)
    :param extra_keywords: пользовательские ключевые слова (регистр не важен)
    """

    if documents_df is None or "Назначение" not in documents_df.columns:
        return pd.DataFrame(columns=NLP_COLUMNS)

    compiled: list[tuple[str, re.Pattern, str]] = [
        (category, re.compile(pattern), marker)
        for category, pattern, marker in RISK_PATTERNS
    ]

    custom: list[tuple[str, re.Pattern]] = []
    if extra_keywords:
        for kw in extra_keywords:
            word = _normalize_text(kw)
            if word:
                custom.append((kw, re.compile(re.escape(word))))

    rows: list[dict] = []
    for _, r in documents_df.iterrows():
        purpose = _normalize_text(r.get("Назначение", ""))
        if not purpose:
            continue

        hits: list[tuple[str, list[str]]] = []

        # Проверяем стандартные паттерны
        for category, pattern, marker in compiled:
            if pattern.search(purpose):
                # Просто добавляем человекочитаемый маркер из RISK_PATTERNS
                hits.append((category, [marker]))

        # Проверяем пользовательские слова
        for original_kw, pattern in custom:
            if pattern.search(purpose):
                hits.append((CUSTOM_CATEGORY, [original_kw])) # Без слешей!

        if not hits:
            continue

        snippet = _snippet(str(r.get("Назначение", "")).strip())
        for category, markers in hits:
            rows.append({
                "Дата": r.get("Дата", ""),
                "Документ": r.get("Документ", ""),
                "Контрагент": r.get("Контрагент", ""),
                "Вид": r.get("Вид", ""),
                # float() защитит от проброса numpy-типов в JSON при сохранении в SQLite
                "Сумма": float(r.get("Сумма", 0.0)),
                "Комментарий": (
                    f"[{category}] {'; '.join(markers)} "
                    f"— назначение платежа: «{snippet}»"
                ),
            })

    return pd.DataFrame(rows, columns=NLP_COLUMNS)
