import pandas as pd
import pytest

from core.auditor import AutoAuditor1C
from core.ml import (
    detect_amount_anomalies,
    detect_turnover_jumps,
    find_duplicate_counterparties,
)


def docs(rows):
    return pd.DataFrame(
        rows,
        columns=["Дата", "Документ", "Контрагент", "Счет", "Вид", "Сумма"],
    )


def osv(rows):
    return pd.DataFrame(rows, columns=[
        "Период", "Счет", "Субконто", "Тип",
        "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
        "КонецДебет", "КонецКредит",
    ])


# ---------------- Аномалии сумм ----------------
def test_amount_anomaly_detected():
    df = docs([
        ["2026-01-10", "Отгр №1", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-05", "Отгр №3", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-10", "Отгр №4", "Ромашка", "62.01", "отгрузка", 5000000],
    ])
    res = detect_amount_anomalies(df)
    assert len(res) == 1
    assert res.iloc[0]["Контрагент"] == "Ромашка"
    assert res.iloc[0]["Сумма"] == pytest.approx(5000000)
    assert res.iloc[0]["Отклонение"] > 10


def test_amount_flat_history_no_anomaly():
    df = docs([
        ["2026-01-10", "Отгр №1", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-05", "Отгр №3", "Ромашка", "62.01", "отгрузка", 50000],
    ])
    assert detect_amount_anomalies(df).empty


def test_amount_requires_min_ops():
    df = docs([
        ["2026-01-10", "Отгр №1", "Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "Ромашка", "62.01", "отгрузка", 5000000],
    ])
    assert detect_amount_anomalies(df).empty  # меньше min_ops=3


# ---------------- Скачки оборотов ----------------
def test_turnover_jump_detected():
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 1000, 500, 0, 0],
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 10000000, 500, 0, 0],
    ])
    res = detect_turnover_jumps(df, min_abs=1000)
    assert len(res) == 1
    assert res.iloc[0]["Счет"] == "51"
    assert res.iloc[0]["Сумма"] == pytest.approx(10000000 - 1000)


def test_turnover_smooth_growth_no_jump():
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 1000, 500, 0, 0],
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 2000, 500, 0, 0],
    ])
    assert detect_turnover_jumps(df, ratio=20, min_abs=100).empty


def test_turnover_skip_single_period():
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 10000000, 500, 0, 0],
    ])
    assert detect_turnover_jumps(df).empty


# ---------------- Дубли контрагентов ----------------
def test_duplicate_pair_found():
    res = find_duplicate_counterparties(["ООО \"Ромашка\"", "Ромашка, ООО", "ООО Вектор"])
    assert len(res) == 1
    assert res.iloc[0]["Сходство"] >= 90
    assert "Ромашка" in res.iloc[0]["Название А"] and "Ромашка" in res.iloc[0]["Название Б"]


def test_distinct_names_not_flagged():
    res = find_duplicate_counterparties(["ООО Вектор", "ИП Смирнов"])
    assert res.empty


def test_duplicates_skip_dash_and_empty():
    res = find_duplicate_counterparties(["-", "", "ООО Вектор"])
    assert res.empty


def test_duplicates_empty_input():
    assert find_duplicate_counterparties([]).empty


# ---------------- Интеграция с AutoAuditor1C ----------------
def test_ml_checks_enabled_add_findings():
    b = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
    ])
    d = docs([
        ["2026-01-10", "Отгр №1", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-05", "Отгр №3", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-10", "Отгр №4", "ООО Ромашка", "62.01", "отгрузка", 5000000],
        ["2026-02-12", "Отгр №5", "Ромашка, ООО", "62.01", "отгрузка", 10000],
    ])
    auditor = AutoAuditor1C(b, documents_df=d, ml_enabled=True)
    errors = auditor.run_audit()
    titles = [e["title"] for e in errors]
    assert any("ML: нетипичная сумма" in t for t in titles)
    assert any("ML: возможные дубли" in t for t in titles)


def test_ml_checks_disabled_by_default():
    b = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
    ])
    auditor = AutoAuditor1C(b)
    errors = auditor.run_audit()
    assert not any(e["title"].startswith("ML:") for e in errors)


def test_ml_per_check_toggle():
    b = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
        ["2026-02-28", "51", "Расчетный", "A", 0, 0, 50000000, 0, 0, 0],
    ])
    d = docs([
        ["2026-01-10", "Отгр №1", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-05", "Отгр №3", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-10", "Отгр №4", "ООО Ромашка", "62.01", "отгрузка", 5000000],
    ])
    auditor = AutoAuditor1C(
        b, documents_df=d, ml_enabled=True,
        ml_amount_anomalies=False, ml_duplicates=False,
        jump_min_abs=1_000_000,
    )
    errors = auditor.run_audit()
    titles = [e["title"] for e in errors]
    assert any("ML: резкий скачок" in t for t in titles)
    assert not any("ML: нетипичная сумма" in t for t in titles)
    assert not any("ML: возможные дубли" in t for t in titles)


def test_report_and_excel_with_ml():
    b = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
    ])
    d = docs([
        ["2026-01-10", "Отгр №1", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-01-15", "Отгр №2", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-05", "Отгр №3", "ООО Ромашка", "62.01", "отгрузка", 50000],
        ["2026-02-10", "Отгр №4", "ООО Ромашка", "62.01", "отгрузка", 5000000],
    ])
    auditor = AutoAuditor1C(b, documents_df=d, ml_enabled=True)
    auditor.run_audit()
    report = auditor.report()
    assert report["status"] == "warning"
    assert any(report["summary"]["Проверка"].str.startswith("ML:"))
    assert auditor.to_excel().startswith(b"PK")
