import pandas as pd
import pytest

from auditor import AutoAuditor1C, account_group, normalize_balances


def osv(rows, cols=None):
    return pd.DataFrame(rows, columns=cols or [
        "Период", "Счет", "Субконто", "Тип",
        "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
        "КонецДебет", "КонецКредит",
    ])


# ---------------- 4.1 Красное сальдо ----------------
def test_red_balance_active_and_passive():
    df = osv([
        ["2026-01-31", "50", "Касса", "A", 0, 0, 0, 0, 0, 5000],      # A с К -> ошибка
        ["2026-01-31", "66", "Кредит", "P", 0, 0, 0, 0, 100000, 0],    # P с Д -> ошибка
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 500000, 0], # норма
        ["2026-01-31", "70", "Зарплата", "P", 0, 0, 0, 0, 0, 120000],   # норма
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    red = [e for e in errors if "Красное сальдо" in e["title"]]
    assert len(red) == 2
    assert set(red[0]["data"]["Счет"]) | set(red[1]["data"]["Счет"]) == {"50", "66"}


def test_red_balance_net_value_not_raw_columns():
    # Активный счет с КД=1000 и КК=500 -> итог дебетовый, ошибки быть не должно
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 1000, 500],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert not [e for e in errors if "Красное сальдо" in e["title"]]


def test_ap_account_not_red_by_default():
    df = osv([
        ["2026-01-31", "60.01", "Ромашка", "AP", 0, 0, 0, 0, 45000, 30000],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert not [e for e in errors if "Красное сальдо" in e["title"]]


def test_settlement_and_loss_accounts_not_red():
    # 71/73/84 — активно-пассивные в 1С:Бухгалтерия 8.3:
    # перерасход подотчета (К по 71), расчеты по займам (К по 73),
    # непокрытый убыток (Д по 84) — не красное сальдо.
    df = osv([
        ["2026-01-31", "71", "Сидоров", "AP", 0, 0, 0, 0, 0, 1100327],
        ["2026-01-31", "73", "Иванов", "AP", 0, 0, 0, 0, 0, 360],
        ["2026-01-31", "84", "Убыток", "AP", 0, 0, 0, 0, 7118253, 0],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert not [e for e in errors if "Красное сальдо" in e["title"]]


# ---------------- 4.2 Развернутое сальдо ----------------
def test_expanded_balance():
    df = osv([
        ["2026-01-31", "60.01", "Ромашка", "AP", 0, 0, 0, 0, 45000, 30000],
        ["2026-01-31", "62.01", "Смирнов", "AP", 0, 0, 0, 0, 250000, 0],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    exp = [e for e in errors if "Развернутое сальдо" in e["title"]]
    assert len(exp) == 1
    assert list(exp[0]["data"]["Счет"]) == ["60.01"]


def test_expanded_balance_ignores_account_level_ap():
    # У активно-пассивного счета без аналитики одновременные Д/К-остатки нормальны
    df = osv([
        ["2026-01-31", "60", "-", "AP", 0, 0, 0, 0, 45000, 30000],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert not [e for e in errors if "Развернутое сальдо" in e["title"]]


# ---------------- 4.3 Незакрытое сальдо на конец месяца ----------------
def test_unclosed_closing_account():
    df = osv([
        ["2026-01-31", "26", "Общехоз", "A", 0, 0, 0, 0, 120000, 0],
        ["2026-01-31", "44", "Расходы", "A", 0, 0, 0, 0, 0, 0],
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    unclosed = [e for e in errors if "Незакрытое сальдо на конец месяца" in e["title"]]
    assert len(unclosed) == 1
    assert list(unclosed[0]["data"]["Счет"]) == ["26"]


def test_stuck_balance_across_periods():
    df = osv([
        ["2026-01-31", "76.05", "Вектор", "AP", 0, 0, 0, 0, 0, 20000],
        ["2026-02-28", "76.05", "Вектор", "AP", 0, 20000, 0, 0, 0, 20000],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    stuck = [e for e in errors if "Зависшее сальдо" in e["title"]]
    assert len(stuck) == 1
    assert list(stuck[0]["data"]["Счет"]) == ["76.05"]


def test_no_stuck_with_single_period():
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 500000, 0],
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert not [e for e in errors if "Зависшее сальдо" in e["title"]]


# ---------------- 4.4 Счет 000 ----------------
def test_account_000_detected_after_string_conversion():
    # Ключевой баг старой версии: '000' превращался в float и проверка не срабатывала
    df = pd.DataFrame([
        {"Период": "2026-01-31", "Счет": "000", "Субконто": "-", "Тип": "AP",
         "НачалоДебет": 0, "НачалоКредит": 0, "ОборотДебет": 1000, "ОборотКредит": 0,
         "КонецДебет": 1000, "КонецКредит": 0},
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    acc000 = [e for e in errors if "счете 000" in e["title"]]
    assert len(acc000) == 1
    assert list(acc000[0]["data"]["Счет"]) == ["000"]


# ---------------- 4.5 Контрагенты ----------------
def _docs(rows):
    return pd.DataFrame(rows, columns=["Дата", "Документ", "Контрагент", "Счет", "Вид", "Сумма"])


def test_unclosed_settlements_with_documents():
    balances = osv([
        ["2026-02-28", "62.01", "ИП Смирнов", "AP", 0, 0, 0, 0, 100000, 0],
    ])
    docs = _docs([
        ["2026-01-20", "Отгрузка №3", "ИП Смирнов", "62.01", "отгрузка", 250000],
        ["2026-02-15", "Оплата №3", "ИП Смирнов", "51", "оплата", 150000],
    ])
    auditor = AutoAuditor1C(balances, documents_df=docs)
    errors = auditor.run_audit()
    unclosed = [e for e in errors if "не закрыты документами" in e["title"]]
    assert len(unclosed) == 1
    assert list(unclosed[0]["data"]["Субконто"]) == ["ИП Смирнов"]
    assert unclosed[0]["data"].iloc[0]["Сумма"] == pytest.approx(100000)


def test_closed_settlements_not_flagged():
    balances = osv([
        ["2026-02-28", "62.01", "ИП Смирнов", "AP", 0, 0, 0, 0, 0, 0],
    ])
    docs = _docs([
        ["2026-01-20", "Отгрузка №3", "ИП Смирнов", "62.01", "отгрузка", 250000],
        ["2026-02-15", "Оплата №3", "ИП Смирнов", "51", "оплата", 250000],
    ])
    auditor = AutoAuditor1C(balances, documents_df=docs)
    errors = auditor.run_audit()
    assert not [e for e in errors if "не закрыты документами" in e["title"]]


def test_settlements_heuristic_without_documents():
    balances = osv([
        ["2026-01-31", "60.01", "Ромашка", "AP", 0, 0, 0, 0, 45000, 30000],
    ])
    auditor = AutoAuditor1C(balances)
    errors = auditor.run_audit()
    heuristic = [e for e in errors if "без реестра документов" in e["title"]]
    assert len(heuristic) == 1
    assert list(heuristic[0]["data"]["Субконто"]) == ["Ромашка"]


# ---------------- Валидация ----------------
def test_missing_columns_raises():
    with pytest.raises(ValueError, match="Отсутствуют обязательные колонки"):
        AutoAuditor1C(pd.DataFrame({"Счет": ["51"]}))


def test_bad_type_raises():
    df = osv([["2026-01-31", "51", "Расчетный", "X", 0, 0, 0, 0, 100, 0]])
    with pytest.raises(ValueError, match="Недопустимые значения Тип"):
        AutoAuditor1C(df)


def test_non_numeric_raises():
    df = osv([["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, "abc", 0]])
    with pytest.raises(ValueError, match="нечисловые значения"):
        AutoAuditor1C(df)


def test_legacy_two_column_format():
    # Старый формат Счет,Субконто,Тип,Дебет,Кредит
    df = pd.DataFrame([
        {"Счет": "000", "Субконто": "-", "Тип": "AP", "Дебет": 1000.0, "Кредит": 0.0},
    ])
    auditor = AutoAuditor1C(df)
    errors = auditor.run_audit()
    assert [e for e in errors if "счете 000" in e["title"]]


# ---------------- Отчеты ----------------
def test_report_structure_and_excel():
    df = osv([
        ["2026-01-31", "50", "Касса", "A", 0, 0, 0, 0, 0, 5000],
    ])
    auditor = AutoAuditor1C(df)
    auditor.run_audit()
    report = auditor.report()
    assert report["status"] == "warning"
    assert report["total_flags"] == 1
    assert not report["summary"].empty
    assert not report["details"].empty

    xlsx = auditor.to_excel()
    assert xlsx.startswith(b"PK")


def test_report_ok_when_no_errors():
    df = osv([
        ["2026-01-31", "51", "Расчетный", "A", 0, 0, 0, 0, 100000, 0],
    ])
    auditor = AutoAuditor1C(df)
    auditor.run_audit()
    assert auditor.report()["status"] == "ok"


# ---------------- Вспомогательное ----------------
def test_account_group():
    assert account_group("60.01") == "60"
    assert account_group("000") == "000"
    assert account_group("90") == "90"


def test_normalize_balances_keeps_zero_string_account():
    df = pd.read_csv("sample_data.csv", dtype=str)
    norm = normalize_balances(df)
    assert "000" in set(norm["Счет"])
    assert set(norm["Тип"]) <= {"A", "P", "AP"}
