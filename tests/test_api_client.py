from typing import Any, Optional

import pandas as pd
import pytest
import requests

from api_client import OneCClient, OSV_COLUMNS


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
        "Счет": {"Code": code, "Description": f"Счет {code}"},
        "ОстатокДт": 0.0,
        "ОстатокКт": 1000.0,
        "ОборотДт": 500.0,
        "ОборотКт": 300.0,
        "ОстатокДтКонеч": 200.0,
        "ОстатокКтКонеч": 0.0,
    }


FRESH_BASE = "https://1cfresh.com/a/sbm_demo/1962515"
REGISTER_EP = f"{FRESH_BASE}/odata/standard.odata/AccountingRegister_Хозрасчетный/BalanceAndTurnovers(StartPeriod=datetime'2026-01-01T00:00:00', EndPeriod=datetime'2026-06-30T23:59:59')"
CHART_EP = f"{FRESH_BASE}/odata/standard.odata/ChartOfAccounts_Хозрасчетный"


def test_fetch_osv_endpoint_and_schema():
    session = FakeSession(pages={REGISTER_EP: [_make_osv_row("60")]})
    client = OneCClient(FRESH_BASE, "user", "pass")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert list(df.columns) == OSV_COLUMNS
    row = df.iloc[0]
    assert row["Счет"] == "60"
    assert row["Тип"] == "AP"
    assert row["Субконто"] == "-"
    assert row["Период"] == "2026-06-30T23:59:59"
    assert row["НачалоДебет"] == pytest.approx(0.0)
    assert row["НачалоКредит"] == pytest.approx(1000.0)
    assert row["ОборотДебет"] == pytest.approx(500.0)
    assert row["ОборотКредит"] == pytest.approx(300.0)
    assert row["КонецДебет"] == pytest.approx(200.0)
    assert row["КонецКредит"] == pytest.approx(0.0)

    url, params, _ = session.calls[0]
    assert url == REGISTER_EP
    assert params["$expand"] == "Счет"
    assert params["$format"] == "json"


def test_fetch_osv_type_from_plan_of_accounts():
    session = FakeSession(pages={REGISTER_EP: [_make_osv_row("20")]})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")
    assert df.iloc[0]["Тип"] == "A"


def test_fetch_osv_pagination_increments_skip():
    first_page = [_make_osv_row("51") for _ in range(1000)]
    second_page = [_make_osv_row("52") for _ in range(5)]
    session = FakeSession(pages={REGISTER_EP: first_page + second_page})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert len(df) == 1005
    assert session.calls[0][1]["$skip"] == 0
    assert session.calls[1][1]["$skip"] == 1000


def test_fetch_osv_resolves_account_guid_via_chart_of_accounts():
    guid = "c3fbdcaf-b8e1-11e4-8271-001e101f0864"
    record = {
        "Счет_Key": guid,
        "ОстатокДт": 0.0,
        "ОстатокКт": 0.0,
        "ОборотДт": 100.0,
        "ОборотКт": 50.0,
        "ОстатокДтКонеч": 60.0,
        "ОстатокКтКонеч": 0.0,
    }
    chart = [{"Ref_Key": guid, "Code": "62"}]
    session = FakeSession(pages={REGISTER_EP: [record], CHART_EP: chart})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")

    assert df.iloc[0]["Счет"] == "62"
    assert df.iloc[0]["Тип"] == "AP"
    assert any("ChartOfAccounts" in url for url, _, _ in session.calls)


def test_fetch_osv_401_raises_friendly_error():
    err = FakeResponse({"error": "auth"}, status_code=401, url=REGISTER_EP)
    session = FakeSession(pages={REGISTER_EP: []}, errors={REGISTER_EP: err})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    with pytest.raises(ValueError, match="УдаленныйДоступOData"):
        client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")


def test_fetch_osv_404_hints_to_enable_register():
    err = FakeResponse({"error": "not found"}, status_code=404, url=REGISTER_EP)
    session = FakeSession(pages={REGISTER_EP: []}, errors={REGISTER_EP: err})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    with pytest.raises(ValueError, match="Настройка стандартного интерфейса OData"):
        client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")


def test_osv_feeds_auditor():
    from auditor import AutoAuditor1C

    record = _make_osv_row("20")
    record["ОстатокДт"] = 0.0
    record["ОстатокКт"] = 100.0
    record["ОборотДт"] = 0.0
    record["ОборотКт"] = 0.0
    record["ОстатокДтКонеч"] = 0.0
    record["ОстатокКтКонеч"] = 100.0

    session = FakeSession(pages={REGISTER_EP: [record]})
    client = OneCClient(FRESH_BASE, "u", "p")
    client.session = session

    df = client.fetch_osv("2026-01-01T00:00:00", "2026-06-30T23:59:59")
    errors = AutoAuditor1C(df).run_audit()

    red = [e for e in errors if e["title"].startswith("Красное сальдо")]
    assert red and "20" in set(red[0]["data"]["Счет"])
