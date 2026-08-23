import pandas as pd
import pytest

from core.auditor import AutoAuditor1C, normalize_documents
from core.nlp import NLP_COLUMNS, detect_payment_risks


def docs(rows):
    return pd.DataFrame(
        rows,
        columns=["Дата", "Документ", "Контрагент", "Счет", "Вид", "Сумма", "Назначение"],
    )


def osv(rows):
    return pd.DataFrame(rows, columns=[
        "Период", "Счет", "Субконто", "Тип",
        "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
        "КонецДебет", "КонецКредит",
    ])


# ---------------- Детекция по категориям ----------------
def test_obnal_detected():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "60.01", "оплата", 500000, "Обнал по чеку"],
    ])
    res = detect_payment_risks(df)
    assert len(res) == 1
    row = res.iloc[0]
    assert row["Контрагент"] == "ООО Тень"
    assert row["Комментарий"].startswith("[Обнал]")


def test_donation_detected():
    df = docs([
        ["2026-01-11", "Платеж №2", "Фонд Добро", "76.09", "оплата", 70000,
         "Благотворительная помощь"],
    ])
    res = detect_payment_risks(df)
    assert len(res) == 1
    assert res.iloc[0]["Комментарий"].startswith("[Пожертвования]")


def test_loan_without_contract_flagged():
    df = docs([
        ["2026-01-12", "Платеж №3", "ООО Лютик", "66.01", "оплата", 100000, "выдача займа"],
    ])
    res = detect_payment_risks(df)
    assert len(res) == 1
    assert res.iloc[0]["Комментарий"].startswith("[Займы без договора]")


def test_loan_with_contract_not_flagged():
    df = docs([
        ["2026-01-13", "Платеж №4", "ООО Лютик", "66.01", "оплата", 100000,
         "выдача займа по договору №12 от 05.01.2026"],
    ])
    assert detect_payment_risks(df).empty


def test_vague_purpose_flagged():
    df = docs([
        ["2026-01-14", "Платеж №5", "ИП Котов", "62.01", "отгрузка", 12000, "За услуги"],
    ])
    res = detect_payment_risks(df)
    assert len(res) == 1
    assert res.iloc[0]["Комментарий"].startswith("[Расплывчатое назначение]")


def test_specific_purpose_clean():
    df = docs([
        ["2026-01-15", "Платеж №6", "ООО Ромашка", "60.01", "оплата", 30000,
         "Оплата за услуги по договору №15 от 03.02.2026"],
    ])
    assert detect_payment_risks(df).empty


# ---------------- Поведение ----------------
def test_missing_column_returns_empty():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "60.01", "оплата", 500000, "Обнал"],
    ]).drop(columns=["Назначение"])
    res = detect_payment_risks(df)
    assert res.empty
    assert list(res.columns) == NLP_COLUMNS


def test_case_insensitive():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "60.01", "оплата", 500000,
         "ОБНАЛИЧИВАНИЕ средств"],
    ])
    assert len(detect_payment_risks(df)) == 1


def test_extra_keywords():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Импекс", "41.01", "оплата", 90000,
         "Серый импорт оборудования"],
    ])
    assert detect_payment_risks(df, extra_keywords=["серый импорт"]).empty is False


def test_extra_keywords_no_false_positive_without_match():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Ромашка", "60.01", "оплата", 90000,
         "Оплата по счету №45"],
    ])
    assert detect_payment_risks(df, extra_keywords=["серый импорт"]).empty


def test_two_categories_two_rows():
    df = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "50.01", "оплата", 500000,
         "обнал за наличный расчет"],
    ])
    res = detect_payment_risks(df)
    cats = [r["Комментарий"].split("]")[0] for _, r in res.iterrows()]
    assert len(res) == 2
    assert "[Обнал" in cats
    assert "[Наличные" in cats


def test_normalize_documents_keeps_and_cleans_purpose():
    raw = pd.DataFrame([{
        "Дата": "2026-01-10",
        "Документ": "Платеж №1",
        "Контрагент": "ООО Тень",
        "Вид": "ОПЛАТА",
        "Сумма": "500000",
        "Назначение платежа": "  Обнал  ",
    }])
    norm = normalize_documents(raw)
    assert "Назначение" in norm.columns
    assert norm.iloc[0]["Назначение"] == "Обнал"


# ---------------- Интеграция с AutoAuditor1C ----------------
NLP_TITLE = "NLP: подозрительные назначения платежей (115-ФЗ)"


@pytest.fixture
def minimal_osv():
    return osv([
        ["2026-01-31", "51", "", "A", 0, 0, 0, 0, 0, 0],
    ])


def test_auditor_includes_nlp_findings(minimal_osv):
    d = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "60.01", "оплата", 500000, "Обнал"],
        ["2026-01-11", "Платеж №2", "ООО Ромашка", "60.01", "оплата", 30000,
         "Оплата по счету №45"],
    ])
    auditor = AutoAuditor1C(minimal_osv, d, ml_enabled=False)
    auditor.run_audit()
    nlp_findings = [e for e in auditor.errors if e["title"] == NLP_TITLE]
    assert len(nlp_findings) == 1
    assert nlp_findings[0]["level"] == "warning"

    summary = auditor.summary_df()
    assert (summary["Проверка"] == NLP_TITLE).any()
    details = auditor.details_df()
    assert (details["Проверка"] == NLP_TITLE).any()


def test_auditor_nlp_disabled(minimal_osv):
    d = docs([
        ["2026-01-10", "Платеж №1", "ООО Тень", "60.01", "оплата", 500000, "Обнал"],
    ])
    auditor = AutoAuditor1C(minimal_osv, d, ml_enabled=False, nlp_enabled=False)
    auditor.run_audit()
    assert all(e["title"] != NLP_TITLE for e in auditor.errors)


def test_auditor_without_documents_no_nlp(minimal_osv):
    auditor = AutoAuditor1C(minimal_osv, None, ml_enabled=False)
    auditor.run_audit()
    assert all(e["title"] != NLP_TITLE for e in auditor.errors)


def test_recommendations_present():
    from core.auditor import RECOMMENDATIONS
    assert NLP_TITLE in RECOMMENDATIONS
