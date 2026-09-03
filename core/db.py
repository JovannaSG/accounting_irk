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

# Путь к конфигу пользователей (роли + доступ к базам)
# По умолчанию — users.json в корне проекта
# Опционально переопределяется переменной окружения AUDIT_USERS_CONFIG
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS_CONFIG_PATH = os.environ.get(
    "AUDIT_USERS_CONFIG",
    os.path.join(_PROJECT_ROOT, "users.json")
)


def init_db():
    """
    Создает таблицы, если их нет, и добавляет недостающие колонки
    (миграция старых БД без findings_json/meta_json/user)

    Таблица `users` хранит роли и доступ к базам (ТЗ §11, доступ бухгалтеров
    к назначенным базам). Если таблица пуста — засевается из конфига users.json
    """

    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    # WAL: многие читатели одновременно с одним писателем
    # (несколько сессий Streamlit / процессов)
    # synchronous=NORMAL безопасен в WAL-режиме и
    # сильно снижает оверхед по диску.
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
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
            user TEXT,
            source_type TEXT,
            source_url TEXT
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
    if "source_type" not in existing:
        cursor.execute("ALTER TABLE audits ADD COLUMN source_type TEXT")
    if "source_url" not in existing:
        cursor.execute("ALTER TABLE audits ADD COLUMN source_url TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            allowed_urls TEXT,
            created_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()

    # Сид первого набора пользователей, если БД пуста
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    if count == 0:
        _seed_users_from_config(cursor)
        conn.commit()

    conn.close()


def load_users_config() -> dict:
    """
    Читает users.json в {login: {...или строка хэша}}. Если файла нет — {}
    """

    if not os.path.exists(USERS_CONFIG_PATH):
        return {}
    try:
        with open(USERS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _seed_users_from_config(cursor) -> None:
    """
    Заполняет пустую таблицу users из users.json
    """

    config = load_users_config()

    for login, spec in config.items():
        if not isinstance(spec, dict) or not login.strip():
            continue
        role = str(spec.get("role") or "accountant").strip().lower()

        pwd_hash = str(
            spec.get("password_hash")
            or spec.get("password")
            or ""
        ).strip()
        if not pwd_hash:
            continue

        allowed = spec.get("allowed_urls") or []
        insert_user(
            cursor,
            login=login.strip().lower(),
            role=role,
            password_hash=pwd_hash,
            allowed_urls=allowed if isinstance(allowed, list) else [],
        )


def insert_user(
    cursor,
    login: str,
    role: str,
    password_hash: str,
    allowed_urls: list,
) -> None:
    """
    Вставляет пользователя. SQLite поддерживает UPSERT начиная с 3.24;
    для совместимости используем INSERT OR REPLACE
    """

    cursor.execute(
        "INSERT OR REPLACE INTO users "
        "(login, role, password_hash, allowed_urls, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (
            login.strip().lower(),
            role,
            password_hash,
            json.dumps(list(allowed_urls or []), ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def upsert_user(
    login: str,
    role: str,
    password_hash: str,
    allowed_urls: list,
    active: bool = True,
) -> None:
    """
    Сохраняет/обновляет пользователя в БД (используется при загрузке из конфига)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (login, role, password_hash, allowed_urls, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(login) DO UPDATE SET "
        "role=excluded.role, password_hash=excluded.password_hash, "
        "allowed_urls=excluded.allowed_urls, active=excluded.active",
        (
            login.strip().lower(),
            role,
            password_hash,
            json.dumps(list(allowed_urls or []), ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
            int(bool(active)),
        ),
    )
    conn.commit()
    conn.close()


def get_user(login: str) -> dict | None:
    """
    Возвращает пользователя по логину или None
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT login, role, password_hash, allowed_urls, active "
        "FROM users WHERE login = ?",
        (str(login).strip().lower(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    urls = []
    try:
        parsed = json.loads(row[3]) if row[3] else []
        if isinstance(parsed, list):
            urls = parsed
    except (TypeError, ValueError):
        urls = []
    return {
        "login": row[0],
        "role": row[1],
        "password_hash": row[2],
        "allowed_urls": urls,
        "active": bool(row[4]),
    }


def list_users() -> list[dict]:
    """
    Возвращает список всех пользователей (без конфиденциальной части)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT login, role, allowed_urls, active FROM users ORDER BY login")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "login": r[0],
            "role": r[1],
            "allowed_urls": _parse_urls(r[2]),
            "active": bool(r[3]),
        }
        for r in rows
    ]


def _parse_urls(raw) -> list:
    try:
        parsed = json.loads(raw) if raw else []
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _json_default(value):
    """
    Сериализатор для дат/спецтипов pandas внутри находок
    """

    if isinstance(value, (datetime, date)):
        return value.isoformat()
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
    деталей. Возвращает None, если данных недостаточно
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

    from core.auth import _normalize_url

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
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

    # Происхождение записи (для фильтрации доступа по базам):
    # source_type: file | mock | odata | batch;
    # source_url — URL базы (для odata/batch)
    _source = result.get("source") or {}
    if not isinstance(_source, dict):
        _source = {}
    source_type = str(_source.get("source_type") or "") \
        or str((meta_payload or {}).get("source_type") or "") \
        or str(result.get("source_type") or "")
    source_url = str((meta_payload or {}).get("url") or "") \
        or str(_source.get("url") or "") \
        or str(result.get("source_url") or "")
    if source_url:
        source_url = _normalize_url(source_url)
    else:
        source_url = None

    cursor.execute("""
        INSERT OR REPLACE INTO audits
        (audit_id, db_name, accountant, viewed_at, status, status_label,
         total_flags, details_json, findings_json, meta_json, user,
         source_type, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        str(source_type) or None,
        str(source_url) or None,
    ))
    conn.commit()
    conn.close()


def load_audit_history(
    user: str | None = None,
    allowed_urls: list | None = None
) -> list[dict]:
    """
    Загружает историю с жестко заданным порядком колонок.

    Если задан `user`:
      - для роли admin (allowed_urls пуст) — возвращаются все записи;
      - для accountant — возвращаются только записи его баз (по source_url из
        allowed_urls) плюс его собственные локальные/файловые прогоны
        (record user == login)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()

    params: list = []
    where = ""
    if user:
        if allowed_urls:
            allow_urls = [u.rstrip("/") for u in allowed_urls if u]
            placeholders = ",".join("?" for _ in allow_urls)
            # accountant: свои локальные (source_url пуст/локальный тип) ИЛИ
            # записи по доступным базам (source_url в списке) ИЛИ
            # записи, созданные самим пользователем вне баз
            where = (
                " WHERE (source_url IN ({ph})"
                " OR user = ?)"
            ).format(ph=placeholders)
            params = list(allow_urls) + [user]
        else:
            # admin (allowed_urls пуст) — видит всё.
            where = ""

    # Явно указываем колонки, чтобы row[7] всегда был details_json
    # user, source_type, source_url — в конце,
    # чтобы не сдвинуть позиционные индексы
    cursor.execute(
        """
        SELECT audit_id, db_name, accountant, viewed_at,
               status, status_label, total_flags, details_json,
               findings_json, meta_json, user, source_type, source_url
        FROM audits
        """ + where + """
        ORDER BY viewed_at ASC
        """,
        params,
    )
    rows = cursor.fetchall()

    history = []
    for row in rows:
        details_df = pd.DataFrame()
        if row[7]:
            try:
                # StringIO защищает от FutureWarnings в новых версиях pandas
                details_df = pd.read_json(
                    io.StringIO(row[7]),
                    orient="records"
                )
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
        if len(row) > 11 and row[11]:
            entry["source_type"] = row[11]
        if len(row) > 12 and row[12]:
            entry["source_url"] = row[12]

        history.append(entry)

    conn.close()
    return history
