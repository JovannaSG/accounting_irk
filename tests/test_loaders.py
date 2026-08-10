from pathlib import Path

import pandas as pd
import pytest

from core.auditor import AutoAuditor1C
from core.loaders import (
    _to_number,
    detect_format,
    extract_osv,
    load_osv_file,
)

FIXTURES = Path(__file__).parent / "fixtures"
REAL_XLS = FIXTURES / "osv_real.xls"
INDICATOR_XLS = FIXTURES / "osv_indicators.xls"


# ---------------- Определение формата ----------------
def test_detect_xls_by_magic():
    data = open(REAL_XLS, "rb").read()
    assert detect_format("anything.dat", data) == "xls"
    assert detect_format("New.xls", data) == "xls"


def test_detect_xlsx():
    data = b"PK\x03\x04 rest of zip"
    assert detect_format("osv.xlsx", data) == "xlsx"


def test_detect_html_extension_and_content():
    assert detect_format("osv.html", b"<html><body><table>") == "html"
    # 1С часто сохраняет HTML с расширением .xls
    assert detect_format("osv.xls", b"<html><table><tr><td>x</td></tr></table>") == "html"


def test_detect_mxl():
    assert detect_format("osv.mxl", b"\x00\x01") == "mxl"


def test_detect_csv():
    assert detect_format("osv.csv", b"a,b,c\n1,2,3") == "csv"


# ---------------- Парсинг чисел ----------------
@pytest.mark.parametrize("raw,expected", [
    ("112,748,946.35", 112748946.35),
    ("798,243.56", 798243.56),
    ("557,140.08", 557140.08),
    ("1.234,56", 1234.56),       # русская локаль: . тысячи, , дробь
    ("1 234 567,89", 1234567.89),
    ("-6,431,831.28", -6431831.28),
    ("500000.0", 500000.0),
    ("—", 0.0),
    ("", 0.0),
    (1234.5, 1234.5),
    (None, 0.0),
])
def test_to_number(raw, expected):
    assert _to_number(raw) == pytest.approx(expected)


# ---------------- Реальный файл 1С (XLS) ----------------
def test_load_real_xls_basics():
    df, info = load_osv_file(REAL_XLS.name, open(REAL_XLS, "rb").read())
    assert info["period"] == "1-st half year of 2026"
    assert len(df) == 67
    assert list(df.columns) == [
        "Период", "Счет", "Субконто", "Тип",
        "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
        "КонецДебет", "КонецКредит",
    ]


def test_load_real_xls_values_and_account_000():
    df, _ = load_osv_file(REAL_XLS.name, open(REAL_XLS, "rb").read())
    acc000 = df[df["Счет"] == "000"].iloc[0]
    assert acc000["НачалоДебет"] == pytest.approx(112748946.35)
    assert acc000["ОборотКредит"] == pytest.approx(798243.56)
    assert acc000["КонецДебет"] == pytest.approx(111950702.79)


def test_load_real_xls_account_codes_and_subconto():
    df, _ = load_osv_file(REAL_XLS.name, open(REAL_XLS, "rb").read())
    codes = set(df["Счет"])
    assert "76.АВ" in codes      # код с буквой
    assert "68.04.1" in codes    # трехуровневый
    assert "Total" not in codes  # итоговая строка отброшена

    sub51 = df[(df["Счет"] == "51") & (df["Субконто"] != "-")]
    assert len(sub51) == 3  # банковские счета как аналитика
    assert "БАЙКАЛЬСКИЙ" in sub51.iloc[0]["Субконто"]


def test_load_real_xls_types_via_plan():
    df, _ = load_osv_file(REAL_XLS.name, open(REAL_XLS, "rb").read())
    types = df.set_index("Счет")["Тип"]
    assert types["71"] == "AP"   # план: подотчетные — активно-пассивный (1С 8.3)
    assert types["84"] == "AP"   # план: непокрытый убыток — АП, не красное сальдо
    assert types["57"] == "A"    # план: переводы в пути — активный
    assert types["60"] == "AP"
    assert types["73"] == "AP"
    assert types["69"] == "AP"


def test_plan_of_accounts_matches_1c83():
    """Ключевые счета должны соответствовать плану счетов 1С:Бухгалтерия 8 ред. 3.0."""
    from core.loaders import PLAN_OF_ACCOUNTS
    ap = {"16", "40", "60", "62", "68", "69", "70", "71", "73",
          "75", "76", "79", "84", "90", "91", "96", "99"}
    assert {c: PLAN_OF_ACCOUNTS[c] for c in sorted(ap)} == {c: "AP" for c in sorted(ap)}
    assert PLAN_OF_ACCOUNTS["66"] == "P"
    assert PLAN_OF_ACCOUNTS["67"] == "P"
    assert PLAN_OF_ACCOUNTS["51"] == "A"
    assert PLAN_OF_ACCOUNTS["20"] == "A"


def test_real_xls_audit_finds_problems():
    df, _ = load_osv_file(REAL_XLS.name, open(REAL_XLS, "rb").read())
    errors = AutoAuditor1C(df).run_audit()

    red = [e for e in errors if "Красное сальдо" in e["title"]]
    red_accounts = set()
    for e in red:
        red_accounts |= set(e["data"]["Счет"])
    assert "20" in red_accounts   # отрицательный остаток в Дебете
    assert "71" not in red_accounts  # перерасход подотчета на АП — не красное сальдо
    assert "84" not in red_accounts  # непокрытый убыток на АП — не красное сальдо

    acc000 = [e for e in errors if "счете 000" in e["title"]]
    assert acc000 and "000" in set(acc000[0]["data"]["Счет"])

    # Аналитики в реальной ОСВ нет -> развернутое сальдо не должно находиться
    assert not [e for e in errors if "Развернутое сальдо" in e["title"]]


# ---------------- ОСВ с показателями БУ/НУ/БУ-НУ ----------------
def test_load_indicator_osv_keeps_only_bu():
    df, info = load_osv_file(INDICATOR_XLS.name, open(INDICATOR_XLS, "rb").read())
    assert info["period"] == "1-st half year of 2026"

    # данные на одну колонку правее: извлекается только БУ
    acc000 = df[df["Счет"] == "000"].iloc[0]
    assert acc000["НачалоДебет"] == pytest.approx(112748946.35)
    assert acc000["ОборотКредит"] == pytest.approx(798243.56)
    assert acc000["КонецДебет"] == pytest.approx(111950702.79)

    # НУ/БУ-НУ/Вал. строки не должны попасть ни как счета, ни как субконто
    assert not df["Субконто"].isin(["НУ", "БУ-НУ", "Вал."]).any()
    assert "НУ" not in set(df["Счет"])


def test_load_indicator_osv_currency_subconto():
    df, _ = load_osv_file(INDICATOR_XLS.name, open(INDICATOR_XLS, "rb").read())
    sub62 = df[(df["Счет"] == "62") & (df["Субконто"] != "-")].set_index("Субконто")
    # валютные субконто сохраняются (это аналитика по БУ), руб. строки тоже
    assert sub62.loc["CNY", "КонецДебет"] == pytest.approx(11990206.01)
    assert sub62.loc["руб.", "КонецДебет"] == pytest.approx(5504050.00)
    # родительская строка включает итоги по субконто
    parent62 = df[(df["Счет"] == "62") & (df["Субконто"] == "-")].iloc[0]
    assert parent62["КонецДебет"] == pytest.approx(67394307.52)


def test_load_indicator_osv_matches_bu_only_report():
    def parents(fn):
        df, _ = load_osv_file(fn.name, open(fn, "rb").read())
        df = df[df["Субконто"] == "-"]
        return df.set_index("Счет")

    real, ind = parents(REAL_XLS), parents(INDICATOR_XLS)
    numeric = ["НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
               "КонецДебет", "КонецКредит"]
    common = set(real.index) & set(ind.index)
    assert common  # есть пересечение счетов
    for code in common:
        for col in numeric:
            assert real.loc[code, col] == pytest.approx(ind.loc[code, col]), (code, col)


def test_load_indicator_osv_audit_finds_core_problems():
    df, _ = load_osv_file(INDICATOR_XLS.name, open(INDICATOR_XLS, "rb").read())
    errors = AutoAuditor1C(df).run_audit()
    titles = [e["title"] for e in errors]
    assert any("счете 000" in t for t in titles)
    assert any("Красное сальдо" in t for t in titles)
    assert any("закрываемые счета" in t for t in titles)


# ---------------- HTML ----------------
HTML_OSV = """<html><head><meta charset="windows-1251"><title>Оборотно-сальдовая ведомость</title></head>
<body>
<div>Оборотно-сальдовая ведомость за Январь 2026</div>
<table border="1">
<tr>
<td rowspan="2">Счет</td><td rowspan="2">Наименование счета</td><td rowspan="2">Показатели</td>
<td colspan="2">Сальдо на начало периода</td><td colspan="2">Обороты за период</td>
<td colspan="2">Сальдо на конец периода</td></tr>
<tr><td>Дебет</td><td>Кредит</td><td>Дебет</td><td>Кредит</td><td>Дебет</td><td>Кредит</td></tr>
<tr><td>51</td><td>Расчетные счета</td><td>БУ</td><td>1 000,00</td><td></td><td>500,00</td><td>600,00</td><td>900,00</td><td></td></tr>
<tr><td></td><td></td><td>НУ</td><td>1 000,00</td><td></td><td>500,00</td><td>600,00</td><td>900,00</td><td></td></tr>
<tr><td></td><td></td><td>БУ-НУ</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>60</td><td>Поставщики</td><td>БУ</td><td>100,00</td><td>200,00</td><td></td><td></td><td>100,00</td><td>200,00</td></tr>
<tr><td>Total</td><td></td><td>БУ</td><td>1 100,00</td><td>200,00</td><td></td><td></td><td>1 000,00</td><td>200,00</td></tr>
</table></body></html>"""


def test_load_html_osv():
    data = HTML_OSV.encode("cp1251")
    df, info = load_osv_file("osv.html", data)
    assert info["period"] == "Январь 2026"
    assert set(df["Счет"]) == {"51", "60"}
    row51 = df[df["Счет"] == "51"].iloc[0]
    assert row51["НачалоДебет"] == pytest.approx(1000.0)
    assert row51["КонецДебет"] == pytest.approx(900.0)
    assert df[df["Счет"] == "60"].iloc[0]["Тип"] == "AP"


def test_load_html_with_xls_extension():
    df, _ = load_osv_file("osv.xls", HTML_OSV.encode("utf-8"))
    assert set(df["Счет"]) == {"51", "60"}


def test_load_html_osv_with_indicators_keeps_bu():
    data = HTML_OSV.encode("cp1251")
    df, _ = load_osv_file("osv.html", data)
    row51 = df[df["Счет"] == "51"].iloc[0]
    assert row51["КонецДебет"] == pytest.approx(900.0)  # БУ, а не НУ


# ---------------- Ошибки и эвристика ----------------
def test_mxl_raises_friendly_error():
    with pytest.raises(ValueError, match="MXL"):
        load_osv_file("osv.mxl", b"\x00\x01\x02")


def test_garbage_raises():
    with pytest.raises(ValueError, match="формат"):
        load_osv_file("file.bin", b"\x00\xff\x00\xff random")


def test_unknown_account_type_by_heuristic():
    grid = pd.DataFrame([
        ["Оборотно-сальдовая ведомость", None, None, None, None, None, None, None],
        ["Счет", "Наименование", "СНД", "СНК", "ОД", "ОК", "СКД", "СКК"],
        ["999", "Неизвестный счет", None, None, None, None, "10,00", None],
    ])
    df, _ = extract_osv(grid)
    assert df.iloc[0]["Тип"] == "A"  # только дебетовый остаток -> активный


def test_plan_override():
    data = open(REAL_XLS, "rb").read()
    df, _ = load_osv_file(REAL_XLS.name, data, plan_override="71:P, 999:A")
    assert df[df["Счет"] == "71"].iloc[0]["Тип"] == "P"
