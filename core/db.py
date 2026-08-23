import sqlite3
import pandas as pd
from datetime import datetime
import os
import io

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
    Создает таблицу, если её нет
    """

    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audits (
            audit_id TEXT PRIMARY KEY,
            db_name TEXT,
            accountant TEXT,
            viewed_at TEXT,
            status TEXT,
            status_label TEXT,
            total_flags INTEGER,
            details_json TEXT
        )
    ''')
    conn.commit()
    conn.close()


def save_audit_log(result: dict) -> None:
    """
    Сохраняет результат аудита в БД
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()

    details_df = result.get("details", pd.DataFrame())
    details_json = details_df.to_json(orient="records", date_format="iso")

    cursor.execute('''
        INSERT OR REPLACE INTO audits
        (audit_id, db_name, accountant, viewed_at, status, status_label, total_flags, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        result.get("audit_id", ""),
        result.get("db_name", "Неизвестная база"),
        result.get("accountant", ""),
        result.get("viewed_at", datetime.now().strftime("%d.%m.%Y %H:%M")),
        result.get("status", ""),
        result.get("status_label", ""),
        result.get("total_flags", 0),
        details_json
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
    cursor.execute("""
        SELECT audit_id, db_name, accountant, viewed_at,
               status, status_label, total_flags, details_json
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

        history.append({
            "audit_id": row[0],
            "db_name": row[1],
            "accountant": row[2],
            "viewed_at": row[3],
            "status": row[4],
            "status_label": row[5],
            "total_flags": row[6],
            "details": details_df
        })

    conn.close()
    return history
