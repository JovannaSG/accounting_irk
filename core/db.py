import sqlite3
import pandas as pd
import io
import json
import os
from datetime import date, datetime

# Позволяем тестам использовать временный файл через переменную окружения
_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "audit_history.db"
    )
)


def init_db():
    """
    Создает таблицу, если её нет, и добавляет недостающие колонки
    (миграция старых БД без findings_json/meta_json/user)
    """

    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audits (
            audit_id TEXT PRIMARY KEY,
            db_name TEXT,
            accountant TEXT,
            viewed_at TEXT,
            status TEXT,
            status_label TEXT,
            total_flags INTEGER,
            details_json TEXT,
            findings_json TEXT,
            meta_json TEXT,
            user TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(audits)")
    existing = {row[1] for row in cursor.fetchall()}
    if "findings_json" not in existing:
        cursor.execute("ALTER TABLE audits ADD COLUMN findings_json TEXT")
    if "meta_json" not in existing:
        cursor.execute("ALTER TABLE audits ADD COLUMN meta_json TEXT")
    if "user" not in existing:
        cursor.execute("ALTER TABLE audits ADD COLUMN user TEXT")
    conn.commit()
    conn.close()


def _json_default(value):
    """
    Сериализатор для дат/спецтипов pandas внутри находок
    """

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (pd.Series, dict)):
        return str(value)
    return str(value)


def _sanitize_finding_records(records: list) -> list:
    """
    NaN/inf/NaT → None, numpy-скаляры → python-типы (для json.dumps)
    """

    from math import isnan

    clean: list = []
    for record in records or []:
        row: dict = {}
        for key, val in dict(record).items():
            # Безопасная проверка на пустоту (ловит NaN, NA, NaT, None)
            if (
                val is pd.NA
                or val is pd.NaT
                or (isinstance(val, float) and isnan(val))
            ):
                val = None
            elif hasattr(val, "item") and not isinstance(val, str):
                try:
                    val = val.item()
                except (ValueError, AttributeError):
                    val = str(val)
            row[str(key)] = val
        clean.append(row)
    return clean


def serialize_errors(errors: list) -> str | None:
    """
    Находки аудитора → JSON-строка [{"title", "level", "amount", "data"}]
    """

    if not errors:
        return None

    payload: list = []
    for e in errors:
        data = e.get("data")
        if isinstance(data, pd.DataFrame):
            records = data.to_dict(orient="records")
        else:
            records = list(data or [])
        payload.append({
            "title": e.get("title", ""),
            "level": e.get("level", ""),
            "amount": float(e.get("amount") or 0.0),
            "data": _sanitize_finding_records(records),
        })
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def deserialize_errors(findings_json: str | None) -> list:
    """
    Обратная операция к serialize_errors; при ошибке парсинга — []
    """

    if not findings_json:
        return []

    try:
        payload = json.loads(findings_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return payload


def rebuild_auditor(entry: dict):
    """
    Восстанавливает аудитора для записи истории (без живого объекта):
    из сохраненных находок, а для старых записей — по плоской таблице
    деталей. Возвращает None, если данных недостаточно.
    """

    from core.auditor import AutoAuditor1C

    if entry.get("auditor") is not None:
        return entry["auditor"]

    errors = entry.get("errors")
    if errors is None:
        details = entry.get("details")
        if details is None or getattr(details, "empty", True):
            return None
        grouped: dict = {}
        for _, row in details.iterrows():
            key = (str(row.get("Проверка", "")), str(row.get("Уровень", "")))
            grouped.setdefault(key, []).append(row)

        errors = []
        for (title, level), rows in grouped.items():
            frame = pd.DataFrame(rows)
            amount = 0.0
            if "Сумма" in frame.columns:
                amount = float(
                    pd.to_numeric(frame["Сумма"], errors="coerce").fillna(0).sum()
                )
            records = frame.drop(
                columns=[c for c in ("Проверка", "Уровень") if c in frame.columns]
            ).to_dict(orient="records")

            # Плоская схема → сальдовые колонки, которые ждут экспорты
            for record in records:
                # Безопасное переименование без создания явных ключей со значением None
                if "Дебет" in record:
                    record["КонецДебет"] = record.pop("Дебет")
                if "Кредит" in record:
                    record["КонецКредит"] = record.pop("Кредит")

            errors.append({
                "title": title,
                "level": level,
                "amount": amount,
                "data": records,
            })

    auditor = AutoAuditor1C.from_findings(errors, entry.get("meta"))
    entry["auditor"] = auditor
    return auditor


def save_audit_log(result: dict) -> None:
    """
    Сохраняет результат аудита в БД (включая находки и реквизиты —
    чтобы экспорт Excel/PDF работал для записей из истории)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()

    details_df = result.get("details", pd.DataFrame())
    details_json = details_df.to_json(orient="records", date_format="iso")

    findings_json = serialize_errors(result.get("errors") or [])
    meta_payload = result.get("meta")
    if not meta_payload:
        auditor = result.get("auditor")
        if auditor is not None:
            meta_payload = getattr(auditor, "meta", None)
        else:
            meta_payload = None

    meta_json = (
        json.dumps(meta_payload, ensure_ascii=False, default=str)
        if meta_payload else None
    )

    cursor.execute("""
        INSERT OR REPLACE INTO audits
        (audit_id, db_name, accountant, viewed_at, status, status_label,
         total_flags, details_json, findings_json, meta_json, user)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result.get("audit_id", ""),
        result.get("db_name", "Неизвестная база"),
        result.get("accountant", ""),
        result.get("viewed_at", datetime.now().strftime("%d.%m.%Y %H:%M")),
        result.get("status", ""),
        result.get("status_label", ""),
        result.get("total_flags", 0),
        details_json,
        findings_json,
        meta_json,
        str(result.get("user") or "") or None,
    ))
    conn.commit()
    conn.close()


def load_audit_history() -> list[dict]:
    """
    Загружает историю с жестко заданным порядком колонок
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()

    # Явно указываем колонки, чтобы row[7] всегда был details_json!
    # user — последняя колонка, чтобы не сдвинуть позиционные индексы.
    cursor.execute("""
        SELECT audit_id, db_name, accountant, viewed_at,
               status, status_label, total_flags, details_json,
               findings_json, meta_json, user
        FROM audits
        ORDER BY viewed_at ASC
    """)
    rows = cursor.fetchall()

    history = []
    for row in rows:
        details_df = pd.DataFrame()
        if row[7]:
            try:
                # StringIO защищает от FutureWarnings в новых версиях pandas
                details_df = pd.read_json(io.StringIO(row[7]), orient="records")
            except Exception:
                pass

        entry: dict = {
            "audit_id": row[0],
            "db_name": row[1],
            "accountant": row[2],
            "viewed_at": row[3],
            "status": row[4],
            "status_label": row[5],
            "total_flags": row[6],
            "details": details_df,
        }

        # Используем deserialize_errors вместо ручного парсинга (DRY)
        if len(row) > 8 and row[8]:
            parsed_errors = deserialize_errors(row[8])
            if parsed_errors:
                entry["errors"] = parsed_errors

        if len(row) > 9 and row[9]:
            try:
                parsed_meta = json.loads(row[9])
                if isinstance(parsed_meta, dict):
                    entry["meta"] = parsed_meta
            except (TypeError, ValueError):
                pass

        if len(row) > 10 and row[10]:
            entry["user"] = row[10]

        history.append(entry)

    conn.close()
    return history
