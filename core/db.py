import sqlite3
import os

import pandas as pd

# База данных будет лежать в корне проекта
DB_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "audit_history.db"
)


def init_db() -> None:
    """
    Создает таблицу для журнала аудита, если ее еще нет.
    """

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXIST audit_logs (
                audit_id TEXT PRIMARY KEY,
                crated_at TEXT,
                db_name TEXT,
                period TEXT,
                accountant TEXT,
                status TEXT,
                total_flags INTEGER,
                total_amount REAL
            )
        """)
        conn.commit()


def save_audit_log(audit_result: dict) -> None:
    """
    Сохраняет метаданные проверки в базу данных
    """

    audit_id = audit_result.get("audit_id")
    created_at = audit_result.get("viewed_at")
    db_name = audit_result.get("db_name")
    accountant = audit_result.get("accountant")
    status = audit_result.get("status_label")
    total_flags = audit_result.get("total_flags", 0)

    # Извлекаем период
    period = audit_result.get("period", "")
    if not period and audit_result.get("auditor"):
        period = audit_result.get("auditor").meta.get("period", "")

    # Считаем общую сумму ошибок, если есть
    details: pd.DataFrame = audit_result.get("details")
    total_amount: float = 0.0
    if (
        details is not None
        and not getattr(details, "empty", True)
        and "Сумма" in details.columns
    ):
        total_amount = float(details["Сумма"].sum())

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
                INSERT OR REPLACE INTO audit_logs
                (audit_id, created_at, db_name, period, accountant, status, total_flags, total_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, created_at,
                db_name, accountant,
                status, total_flags,
                total_amount
            )
        )
        conn.commit()


def get_audit_logs() -> list[dict]:
    """
    Возвращает все записи из журнала, отсортированные от новых к старым
    """

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]
