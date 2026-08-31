import os
import sqlite3

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
        "INSERT INTO audits (audit_id, db_name, accountant, viewed_at, "
        "status, status_label, total_flags, details_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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


# ── 8. Находки и реквизиты сохраняются, экспорт работает из истории ──

def _errors_payload() -> list:
    return [
        {
            "title": "Незакрытое сальдо на конец месяца (закрываемые счета)",
            "level": "error",
            "amount": 5000.0,
            "data": [{
                "Период": "2026-01-31", "Счет": "50", "Субконто": "-",
                "КонецДебет": 0.0, "КонецКредит": 5000.0,
                "Сумма": 5000.0, "Комментарий": "тест",
            }],
        },
    ]


def test_findings_roundtrip_and_export(tmp_db):

    result = _sample_result()
    result["meta"] = {"organization": "ООО Тест", "period": "Январь"}
    result["errors"] = _errors_payload()
    db_mod.save_audit_log(result)

    history = db_mod.load_audit_history()
    entry = history[0]
    assert entry["errors"][0]["title"].startswith("Незакрытое сальдо")
    assert entry["meta"]["organization"] == "ООО Тест"

    auditor = db_mod.rebuild_auditor(entry)
    assert auditor is not None
    assert len(auditor.errors) == 1
    # Экспорт из восстановленного аудитора
    assert auditor.to_excel().startswith(b"PK")
    assert auditor.to_pdf().startswith(b"%PDF")


def test_rebuild_from_legacy_details_only(tmp_db):
    from core.auditor import AutoAuditor1C  # noqa: F401 — проверка типа

    # Старая запись: только details_json, без findings_json/meta_json
    result = _sample_result()
    db_mod.save_audit_log(result)
    history = db_mod.load_audit_history()
    entry = history[0]
    assert "errors" not in entry

    auditor = db_mod.rebuild_auditor(entry)
    assert isinstance(auditor, AutoAuditor1C)
    assert [e["title"] for e in auditor.errors] == ["Красное сальдо"]
    assert auditor.errors[0]["data"].iloc[0]["КонецКредит"] == 500.0
    assert auditor.to_pdf().startswith(b"%PDF")


def test_rebuild_returns_none_without_data(tmp_db):
    result = _sample_result()
    result["details"] = pd.DataFrame()
    result["errors"] = []
    db_mod.save_audit_log(result)
    entry = db_mod.load_audit_history()[0]
    assert db_mod.rebuild_auditor(entry) is None


# ── 9. Пользователь запуска в журнале (ТЗ §11) ──

def test_save_load_user(tmp_db):
    result = _sample_result()
    result["user"] = "auditor"
    db_mod.save_audit_log(result)
    entry = db_mod.load_audit_history()[0]
    assert entry["user"] == "auditor"


def test_user_missing_in_old_style_record(tmp_db):
    result = _sample_result()
    db_mod.save_audit_log(result)
    entry = db_mod.load_audit_history()[0]
    assert "user" not in entry


def test_migration_adds_user_column(tmp_db):
    # БД старого образца: без findings_json/meta_json/user
    conn = sqlite3.connect(tmp_db)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE audits (
            audit_id TEXT PRIMARY KEY,
            db_name TEXT,
            accountant TEXT,
            viewed_at TEXT,
            status TEXT,
            status_label TEXT,
            total_flags INTEGER,
            details_json TEXT
        )
    """)
    cursor.execute(
        "INSERT INTO audits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old-001", "Старая база", "", "01.08.2026", "ok", "ОК", 0, None),
    )
    conn.commit()
    conn.close()

    db_mod.init_db()
    history = db_mod.load_audit_history()
    assert len(history) == 1
    assert history[0]["audit_id"] == "old-001"
    assert "user" not in history[0]
