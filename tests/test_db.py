import os
import sqlite3
import tempfile

import pandas as pd
import pytest

import core.db as db_mod


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Перенаправляет БД в tmp_path на время теста."""
    test_path = str(tmp_path / "test_audit.db")
    monkeypatch.setattr(db_mod, "_DB_PATH", test_path)
    return test_path


def _sample_result(audit_id: str = "test-001") -> dict:
    details = pd.DataFrame([
        {"Проверка": "Красное сальдо", "Уровень": "error",
         "Период": "2026-01-31", "Счет": "51", "Субконто": "-",
         "Договор": "-", "Дебет": 0.0, "Кредит": 500.0,
         "Сумма": 500.0, "Комментарий": "тест"},
    ])
    return {
        "audit_id": audit_id,
        "db_name": "Тестовая база",
        "accountant": "Иванова И.И.",
        "viewed_at": "12.08.2026 10:00",
        "status": "warning",
        "status_label": "Найдены замечания",
        "total_flags": 1,
        "details": details,
    }


# ── 1. init_db создаёт таблицу ──

def test_init_db_creates_table(tmp_db):
    db_mod.init_db()
    conn = sqlite3.connect(tmp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cursor.fetchall()]
    conn.close()
    assert "audits" in tables


# ── 2. save → load roundtrip ──

def test_save_and_load_roundtrip(tmp_db):
    result = _sample_result()
    db_mod.save_audit_log(result)
    history = db_mod.load_audit_history()
    assert len(history) == 1
    row = history[0]
    assert row["audit_id"] == "test-001"
    assert row["db_name"] == "Тестовая база"
    assert row["accountant"] == "Иванова И.И."
    assert row["total_flags"] == 1
    assert not row["details"].empty
    assert str(row["details"].iloc[0]["Счет"]) == "51"


# ── 3. details_json сериализация ──

def test_details_json_serialization(tmp_db):
    result = _sample_result()
    db_mod.save_audit_log(result)
    history = db_mod.load_audit_history()
    details = history[0]["details"]
    assert "Сумма" in details.columns
    assert float(details.iloc[0]["Сумма"]) == 500.0


# ── 4. INSERT OR REPLACE на дублирующемся audit_id ──

def test_save_replaces_existing(tmp_db):
    r1 = _sample_result("dup-001")
    r1["total_flags"] = 1
    db_mod.save_audit_log(r1)

    r2 = _sample_result("dup-001")
    r2["total_flags"] = 5
    db_mod.save_audit_log(r2)

    history = db_mod.load_audit_history()
    assert len(history) == 1
    assert history[0]["total_flags"] == 5


# ── 5. Пустая БД → пустой список ──

def test_load_empty_db(tmp_db):
    db_mod.init_db()
    history = db_mod.load_audit_history()
    assert history == []


# ── 6. Битый JSON details → пустой DataFrame ──

def test_load_corrupt_json(tmp_db):
    db_mod.init_db()
    conn = sqlite3.connect(tmp_db)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO audits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("bad-001", "База", "", "12.08.2026", "ok", "OK", 0, "NOT_JSON{{{"),
    )
    conn.commit()
    conn.close()

    history = db_mod.load_audit_history()
    assert len(history) == 1
    assert history[0]["details"].empty


# ── 7. БД создаётся в tmp_path (не в корне проекта) ──

def test_db_uses_tmp_path(tmp_db):
    db_mod.init_db()

    # Убеждаемся, что БД успешно создалась по переданному временному пути
    assert os.path.exists(tmp_db)
