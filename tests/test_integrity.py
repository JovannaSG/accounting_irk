import pandas as pd

from core.auditor import AutoAuditor1C
from core.integrity import (
    WARN_COLUMN_SHIFT,
    WARN_MAGNITUDE_MISMATCH,
    check_bookkeeping_identity,
    check_magnitude_correlation,
    validate_osv_integrity,
)
from core.loaders import load_osv_file

OSV_COLS = [
    "Период", "Счет", "Субконто", "Тип",
    "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
    "КонецДебет", "КонецКредит", "Договор",
]


def _osv(rows):
    return pd.DataFrame(rows, columns=OSV_COLS)


# ---------------- Проверка 1: математическое тождество ----------------
def test_identity_pass_on_consistent_data():
    # Конец(200) = Начало(100) + ОборотД(50) - ОборотК(?) — подберём согласованные числа.
    df = _osv([
        ["2026-02-28", "50", "Касса", "A", 100, 0, 50, 0, 150, 0, "-"],  # 150=100+50
        ["2026-02-28", "60.01", "Контрагент", "AP", 0, 0, 0, 30, 0, 30, "-"],  # 30 оборотов
    ])
    assert check_bookkeeping_identity(df) == []


def test_identity_fires_on_column_shift():
    # «Обороты» (ОборотДебет=1234570.67) ошибочно попали в «КонецДебет»:
    # тождество рушится почти у всех строк → предупреждение WARN_COLUMN_SHIFT.
    df = _osv([
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 1234570.67, 500000, 0, 0, "-"],
        ["2026-02-28", "60.02", "Контрагент", "AP", 0, 0, 0, 300000, 0, 0, "-"],
    ])
    assert WARN_COLUMN_SHIFT in check_bookkeeping_identity(df)


def test_identity_empty_df():
    assert check_bookkeeping_identity(pd.DataFrame()) == []


# ---------------- Проверка 2: соотношение оборотов и остатков ----------------
def test_magnitude_fires_when_turnover_parsed_as_balance():
    df = _osv([
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 1000000, 0, 5000, 0, "-"],
        ["2026-02-28", "60", "Контрагент", "AP", 0, 0, 500000, 0, 2000, 0, "-"],
    ])
    # Средний оборот ~750k, макс. остаток 5000 → 750k > 50k (5000*10)
    assert WARN_MAGNITUDE_MISMATCH in check_magnitude_correlation(df)


def test_magnitude_silent_on_normal_data():
    df = _osv([
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 10000, 9000, 1000, 0, "-"],
    ])
    assert check_magnitude_correlation(df) == []


def test_magnitude_empty_df():
    assert check_magnitude_correlation(pd.DataFrame()) == []


# ---------------- Агрегация validate_osv_integrity ----------------
def test_validate_return_shape():
    df = _osv([
        ["2026-02-28", "50", "Касса", "A", 100, 0, 50, 0, 150, 0, "-"],
    ])
    report = validate_osv_integrity(df, {"db_name": "x", "source_type": "odata", "period": "P", "organization": "O"})
    assert set(report) == {"integrity_warnings", "provenance", "is_suspicious"}
    assert report["integrity_warnings"] == []
    # Нет фантомного предупреждения: чистые OData-данные не считаются подозрительными.
    assert report["is_suspicious"] is False
    assert report["provenance"]["db_name"] == "x"


def test_validate_clean_not_suspicious():
    df = _osv([
        ["2026-02-28", "50", "Касса", "A", 100, 0, 50, 0, 150, 0, "-"],
    ])
    report = validate_osv_integrity(df, {"db_name": "y", "source_type": "file"})
    assert report["integrity_warnings"] == []
    assert report["is_suspicious"] is False


def test_validate_suspicious_only_on_real_anomaly():
    # Реальная аномалия (сдвиг колонок) по-прежнему помечает данные как подозрительные.
    df = _osv([
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 1234570.67, 500000, 0, 0, "-"],
    ])
    report = validate_osv_integrity(df, {"db_name": "z", "source_type": "odata"})
    assert WARN_COLUMN_SHIFT in report["integrity_warnings"]
    assert report["is_suspicious"] is True


# ---------------- Интеграция с load_osv_file ----------------
def test_file_load_attaches_integrity():
    # CSV-путь в loaders.py возвращает info с полем integrity.
    csv_text = "Счет,Тип,КонецДебет,КонецКредит,Период,Организация\n50,A,0,5000,2026-02-28,O\n"
    df, info = load_osv_file("osv.csv", csv_text.encode("utf-8"))
    assert "integrity" in info
    assert info["integrity"]["provenance"]["db_name"] == "osv.csv"
    assert info["integrity"]["provenance"]["source_type"] == "file"


# ---------------- Фантомное красное сальдо не возникает ----------------
def test_phantom_red_balance_not_created():
    """Корректные данные из файла не должны порождать фантомного красного сальдо
    из-за нормального оборота, ошибочно принятого за остаток."""
    df = _osv([
        ["2026-02-28", "58.03", "Займ Иванову", "A", 0, 0, 1234570.67, 1234570.67, 0, 0, "-"],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    red = [e for e in errors if "Красное сальдо" in e["title"]]
    assert not red
