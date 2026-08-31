import uuid
from typing import Any, Optional

import pytest
import requests

from core.api_client import OneCClient, OSV_COLUMNS


def _guid_for(code: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, code))


def _make_osv_row(code: str = "60") -> dict:
    return {
        "Account_Key": _guid_for(code),
        "СуммаOpeningBalanceDr": 0.0,
        "СуммаOpeningBalanceCr": 1000.0,
        "СуммаTurnoverDr": 500.0,
        "СуммаTurnoverCr": 300.0,
        "СуммаClosingBalanceDr": 200.0,
        "СуммаClosingBalanceCr": 0.0,
    }


def _chart_rows(codes: list[str]) -> list[dict]:
    return [{"Ref_Key": _guid_for(c), "Code": c} for c in codes]


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, url: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Записывает вызовы и раздаёт заранее заданные ответы по URL."""

    def __init__(self, pages: dict[str, list[dict]], errors: Optional[dict[str, FakeResponse]] = None):
        self.pages = pages
        self.errors = errors or {}
        self.calls: list[tuple[str, dict, dict]] = []

    def get(self, endpoint: str, params: dict, timeout: int):
        self.calls.append((endpoint, dict(params), timeout))
        if endpoint in self.errors:
            return self.errors[endpoint]
        page = self.pages.get(endpoint, [])
        skip = params.get("$skip", 0)
        top = params.get("$top", 1000)
        chunk = page[skip:skip + top]
        return FakeResponse({"value": chunk})


def _make_osv_row(code: str = "60") -> dict:
    return {
        "Account_Key": _guid_for(code),
        "СуммаOpeningBalanceDr": 0.0,
        "СуммаOpeningBalanceCr": 1000.0,
        "СуммаTurnoverDr": 500.0,
        "СуммаTurnoverCr": 300.0,
        "СуммаClosingBalanceDr": 200.0,
        "СуммаClosingBalanceCr": 0.0,
    }


def _chart_rows(codes: list[str]) -> list[dict]:
    return [{"Ref_Key": _guid_for(c), "Code": c} for c in codes]


FRESH_BASE = "https://1cfresh.com/a/sbm_demo/1962515"
REGISTER_EP = f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/BalanceAndTurnovers(StartPeriod=datetime'2026-01-01T00:00:00', EndPeriod=datetime'2026-06-30T23:59:59')"
CHART_EP = f"{FRESH_BASE}/odata/standard.odata/ChartOfAccounts_Хозрасчетный"


def test_fetch_osv_endpoint_and_schema():
    session = FakeSession(pages={REGISTER_EP: [_make_osv_row("60")], CHART_EP: _chart_rows(["60"])})
    client = OneCClient(FRESH_BASE, "user", "pass")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert list(df.columns) == OSV_COLUMNS
    row = df.iloc[0]
    assert row["Счет"] == "60"
    assert row["Тип"] == "AP"
    assert row["Субконто"] == "-"
    assert row["Период"] == "2026-06-30"
    assert row["НачалоДебет"] == pytest.approx(0.0)
    assert row["НачалоКредит"] == pytest.approx(1000.0)
    assert row["ОборотДебет"] == pytest.approx(500.0)
    assert row["ОборотКредит"] == pytest.approx(300.0)
    assert row["КонецДебет"] == pytest.approx(200.0)
    assert row["КонецКредит"] == pytest.approx(0.0)

    url, params, _ = session.calls[0]
    assert url == REGISTER_EP
    assert params["$format"] == "json"


def test_fetch_osv_appends_end_of_day_to_bare_dates():
    """Голая дата (без времени) в границах периода дополняется до конца дня,
    чтобы не обрезать регламентные операции 90/91 в 23:59:59."""
    safe_ep = (
        f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/"
        f"BalanceAndTurnovers(StartPeriod=datetime'2026-06-01T00:00:00', "
        f"EndPeriod=datetime'2026-06-30T23:59:59')"
    )
    session = FakeSession(pages={safe_ep: [_make_osv_row("90")], CHART_EP: _chart_rows(["90"])})
    client = OneCClient(FRESH_BASE, "user", "pass")
    client.session = session

    df = client.fetch_osv("2026-06-01", "2026-06-30")

    url, _, _ = session.calls[0]
    assert "EndPeriod=datetime'2026-06-30T23:59:59'" in url
    assert "StartPeriod=datetime'2026-06-01T00:00:00'" in url
    # Колонка «Период» остаётся чистой датой (без времени)
    assert df.iloc[0]["Период"] == "2026-06-30"


def test_fetch_osv_account_subconto_appends_end_of_day_to_bare_date():
    guid = _guid_for("60")
    safe_ep = (
        f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/"
        f"BalanceAndTurnovers(StartPeriod=datetime'2026-02-01T00:00:00', "
        f"EndPeriod=datetime'2026-02-28T23:59:59', "
        f"AccountCondition='Account_Key eq guid''{guid}''')"
    )
    record = {
        "Account_Key": guid,
        "СуммаOpeningBalanceDr": 0.0, "СуммаOpeningBalanceCr": 0.0,
        "СуммаTurnoverDr": 100.0, "СуммаTurnoverCr": 50.0,
        "СуммаClosingBalanceDr": 60.0, "СуммаClosingBalanceCr": 0.0,
    }
    session = FakeSession(pages={safe_ep: [record], CHART_EP: _chart_rows(["60"])})
    client = OneCClient(FRESH_BASE, "user", "pass")
    client.session = session

    df = client.fetch_osv_account_subconto("2026-02-01", "2026-02-28", "60")

    register_url = next(url for url, _, _ in session.calls if "BalanceAndTurnovers" in url)
    assert "EndPeriod=datetime'2026-02-28T23:59:59'" in register_url
    assert "StartPeriod=datetime'2026-02-01T00:00:00'" in register_url
    assert df.iloc[0]["Период"] == "2026-02-28"


def test_fetch_osv_type_from_plan_of_accounts():
    session = FakeSession(pages={REGISTER_EP: [_make_osv_row("20")], CHART_EP: _chart_rows(["20"])})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")
    assert df.iloc[0]["Тип"] == "A"


def test_fetch_osv_pagination_increments_skip():
    first_page = [_make_osv_row("51") for _ in range(1000)]
    second_page = [_make_osv_row("52") for _ in range(5)]
    session = FakeSession(
        pages={REGISTER_EP: first_page + second_page, CHART_EP: _chart_rows(["51", "52"])}
    )
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert len(df) == 1005
    assert session.calls[0][1]["$skip"] == 0
    assert session.calls[1][1]["$skip"] == 1000


def test_fetch_osv_resolves_account_guid_via_chart_of_accounts():
    guid = _guid_for("62")
    record = {
        "Account_Key": guid,
        "СуммаOpeningBalanceDr": 0.0,
        "СуммаOpeningBalanceCr": 0.0,
        "СуммаTurnoverDr": 100.0,
        "СуммаTurnoverCr": 50.0,
        "СуммаClosingBalanceDr": 60.0,
        "СуммаClosingBalanceCr": 0.0,
    }
    chart = [{"Ref_Key": guid, "Code": "62"}]
    session = FakeSession(pages={REGISTER_EP: [record], CHART_EP: chart})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert df.iloc[0]["Счет"] == "62"
    assert df.iloc[0]["Тип"] == "AP"
    assert any("ChartOfAccounts" in url for url, _, _ in session.calls)


def test_fetch_osv_falls_back_to_legacy_field_names():
    """Старые имена полей (самостоятельный 1С) продолжают работать."""
    record = {
        "Счет_Key": _guid_for("51"),
        "ОстатокДт": 10.0,
        "ОстатокКт": 20.0,
        "ОборотДт": 30.0,
        "ОборотКт": 40.0,
        "ОстатокДтКонеч": 50.0,
        "ОстатокКтКонеч": 60.0,
    }
    session = FakeSession(
        pages={REGISTER_EP: [record], CHART_EP: _chart_rows(["51"])}
    )
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    row = df.iloc[0]
    assert row["Счет"] == "51"
    assert row["НачалоДебет"] == pytest.approx(10.0)
    assert row["ОборотКредит"] == pytest.approx(40.0)
    assert row["КонецДебет"] == pytest.approx(50.0)


def test_fetch_osv_401_raises_friendly_error():
    err = FakeResponse({"error": "auth"}, status_code=401, url=REGISTER_EP)
    session = FakeSession(pages={REGISTER_EP: []}, errors={REGISTER_EP: err})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    with pytest.raises(ValueError, match="401 Unauthorized"):
        client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")


def test_fetch_osv_404_hints_to_enable_register():
    err = FakeResponse({"error": "not found"}, status_code=404, url=REGISTER_EP)
    session = FakeSession(pages={REGISTER_EP: []}, errors={REGISTER_EP: err})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    with pytest.raises(ValueError, match="виртуальную таблицу"):
        client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")


def test_osv_feeds_auditor():
    from core.auditor import AutoAuditor1C

    record = _make_osv_row("20")
    record["СуммаOpeningBalanceDr"] = 0.0
    record["СуммаOpeningBalanceCr"] = 100.0
    record["СуммаTurnoverDr"] = 0.0
    record["СуммаTurnoverCr"] = 0.0
    record["СуммаClosingBalanceDr"] = 0.0
    record["СуммаClosingBalanceCr"] = 100.0

    session = FakeSession(pages={REGISTER_EP: [record], CHART_EP: _chart_rows(["20"])})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")
    errors = AutoAuditor1C(df).run_audit()

    red = [e for e in errors if e["title"].startswith("Красное сальдо")]
    assert red and "20" in set(red[0]["data"]["Счет"])


def _osv_endpoint(start: str, end: str) -> str:
    return (
        f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/"
        f"BalanceAndTurnovers(StartPeriod=datetime'{start}', EndPeriod=datetime'{end}')"
    )


def test_fetch_osv_monthly_two_months():
    jan_ep = _osv_endpoint("2026-01-01T00:00:00", "2026-01-31T23:59:59")
    feb_ep = _osv_endpoint("2026-02-01T00:00:00", "2026-02-28T23:59:59")
    session = FakeSession(pages={
        jan_ep: [_make_osv_row("60")],
        feb_ep: [_make_osv_row("51")],
        CHART_EP: _chart_rows(["60", "51"]),
    })
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df, _ = client.fetch_osv_monthly("2026-01-01T00:00:00", "2026-02-28T23:59:59")

    assert list(df.columns) == OSV_COLUMNS
    assert len(df) == 2
    assert set(df["Период"]) == {"2026-01-31", "2026-02-28"}
    assert set(df["Счет"]) == {"60", "51"}
    assert any(jan_ep in url for url, _, _ in session.calls)


def test_fetch_osv_monthly_attaches_integrity():
    """fetch_osv_monthly возвращает кортеж (df, info) с отчётом о целостности."""
    jan_ep = _osv_endpoint("2026-01-01T00:00:00", "2026-01-31T23:59:59")
    session = FakeSession(pages={
        jan_ep: [_make_osv_row("60")],
        CHART_EP: _chart_rows(["60"]),
    })
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df, info = client.fetch_osv_monthly("2026-01-01T00:00:00", "2026-01-31T23:59:59")

    assert not df.empty
    assert isinstance(info, dict)
    assert "integrity" in info
    assert "provenance" in info["integrity"]
    assert info["integrity"]["provenance"]["source_type"] == "odata"


def test_fetch_osv_monthly_partial_last_month():
    """Последний месяц диапазона обрезается до period_end."""
    mar_ep = _osv_endpoint("2026-03-01T00:00:00", "2026-03-15T23:59:59")
    session = FakeSession(pages={
        mar_ep: [_make_osv_row("60")],
        CHART_EP: _chart_rows(["60"]),
    })
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df, _ = client.fetch_osv_monthly("2026-03-01T00:00:00", "2026-03-15T23:59:59")

    assert len(df) == 1
    assert df.iloc[0]["Период"] == "2026-03-15"


def test_fetch_osv_monthly_empty_range():
    jan_ep = _osv_endpoint("2026-01-01T00:00:00", "2026-01-31T23:59:59")
    session = FakeSession(pages={jan_ep: []})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df, info = client.fetch_osv_monthly("2026-01-01T00:00:00", "2026-01-31T23:59:59")

    assert df.empty
    assert list(df.columns) == OSV_COLUMNS
    # Даже при пустом диапазоне возвращается служебный словарь info.
    assert isinstance(info, dict)


def test_fetch_osv_monthly_invalid_range():
    client = OneCClient(FRESH_BASE, "u", "p")
    with pytest.raises(ValueError, match="Некорректный диапазон"):
        client.fetch_osv_monthly("2026-06-30T23:59:59", "2026-01-01T00:00:00")


def test_fetch_osv_account_subconto_filters_and_resolves():
    guid_60 = _guid_for("60")
    subconto_ep = (
        f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/"
        f"BalanceAndTurnovers(StartPeriod=datetime'2026-01-01T00:00:00', "
        f"EndPeriod=datetime'2026-02-28T23:59:59', "
        f"AccountCondition='Account_Key eq guid''{guid_60}''')"
    )

    def subconto_row(code: str, subconto, closing_dr: float, closing_cr: float) -> dict:
        row = _make_osv_row(code)
        row["ExtDimension1"] = subconto
        row["СуммаClosingBalanceDr"] = closing_dr
        row["СуммаClosingBalanceCr"] = closing_cr
        return row

    records = [
        subconto_row("60", {"Наименование": "ООО Ромашка"}, 500.0, 0.0),
        subconto_row("60", "ref-string-контрагент-2", 300.0, 0.0),
        subconto_row("51", {"Наименование": "Банк"}, 1000.0, 0.0),
    ]
    session = FakeSession(pages={
        subconto_ep: records,
        CHART_EP: _chart_rows(["60", "51"]),
    })
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv_account_subconto("2026-01-01T00:00:00", "2026-02-28T23:59:59", "60")

    assert list(df.columns) == OSV_COLUMNS
    assert set(df["Счет"]) == {"60"}
    assert len(df) == 2

    assert set(df["Субконто"]) == {"ООО Ромашка", "ref-string-контрагент-2"}
    assert df.loc[df["Субконто"] == "ООО Ромашка"].iloc[0]["КонецДебет"] == pytest.approx(500.0)

    subconto_calls = [(u, p) for u, p, _ in session.calls if "AccountCondition" in u]
    assert subconto_calls, "запрос с AccountCondition не был отправлен"
    url, params = subconto_calls[0]
    assert url == subconto_ep
    assert "$select" in params, "В запросе отсутствует обязательный параметр $select"
    assert "ExtDimension1" in params["$select"], "В $select не запрошено первое субконто (ExtDimension1)"
    assert "ExtDimension2" in params["$select"], "В $select не запрошено второе субконто (ExtDimension2)"
