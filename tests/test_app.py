import pandas as pd
import pytest

from streamlit.testing.v1 import AppTest

import http_client

APP = "app.py"


def _osv_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["2026-01-31", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
            ["2026-02-28", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
        ],
        columns=[
            "Период", "Счет", "Субконто", "Тип",
            "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
            "КонецДебет", "КонецКредит",
        ],
    )


def _audit_payload(db_name: str = "Тестовая база") -> dict:
    return {
        "audit_id": "test-audit-id",
        "db_name": db_name,
        "status": "warning",
        "status_label": "Обнаружены замечания",
        "total_flags": 1,
        "balances": _osv_df().to_dict(orient="records"),
        "summary": [
            {
                "Имя Базы": db_name,
                "Счет": "20",
                "Вид нарушений": "Красное сальдо",
                "Период(ы)": "2026-01-31, 2026-02-28",
                "Дата просмотра": "10.08.2026 00:00",
            }
        ],
        "details": [
            {
                "Период": "2026-01-31",
                "Счет": "20",
                "Субконто": "-",
                "Проверка": "Красное сальдо",
                "Тип": "A",
                "НачалоДебет": 0.0,
                "НачалоКредит": 100.0,
                "ОборотДебет": 0.0,
                "ОборотКредит": 0.0,
                "КонецДебет": 0.0,
                "КонецКредит": 100.0,
            }
        ],
        "errors": [
            {
                "title": "Красное сальдо (счет 20)",
                "level": "error",
                "amount": 100.0,
                "data": [{"Счет": "20", "Субконто": "-", "КонецДебет": 0.0, "КонецКредит": 100.0}],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _mock_http_client(monkeypatch):
    monkeypatch.setattr(http_client, "check_health", lambda: True)
    monkeypatch.setattr(http_client, "run_audit_1c", lambda *a, **kw: _audit_payload("https://example.com/base"))
    monkeypatch.setattr(http_client, "run_audit_mock", lambda *a, **kw: _audit_payload("Тестовая база"))
    monkeypatch.setattr(http_client, "run_audit_file", lambda *a, **kw: _audit_payload("test.csv"))
    monkeypatch.setattr(http_client, "get_account_detail", lambda *a, **kw: {
        "by_period": _osv_df().to_dict(orient="records"),
        "by_subconto": [],
    })
    monkeypatch.setattr(http_client, "get_excel_report", lambda *a, **kw: b"xlsx-bytes")
    monkeypatch.setattr(http_client, "get_pdf_report", lambda *a, **kw: b"pdf-bytes")
    yield


def test_api_source_loads_osv_and_runs_audit(monkeypatch):
    from api_client import OneCClient
    monkeypatch.setattr(
        OneCClient, "fetch_osv_monthly", lambda self, start, end: _osv_df()
    )

    at = AppTest.from_file(APP, default_timeout=20)
    at.run()
    assert not at.exception

    at.sidebar.radio(key="data_source").set_value("☁️ 1С:Фреш (OData)")
    at.run()
    assert not at.exception

    at.sidebar.text_input(key="api_url").set_value("https://example.com/base")
    at.sidebar.text_input(key="api_user").set_value("odata.user")
    at.sidebar.text_input(key="api_pass").set_value("secret")
    at.sidebar.button(key="btn_fetch").click()
    at.run()
    assert not at.exception

    assert "api_balances" in at.session_state
    assert not at.session_state["api_balances"].empty

    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    assert "audit" in at.session_state
    audit = at.session_state["audit"]
    assert audit["db_name"] == "https://example.com/base"
    assert audit["status"] == "warning"
    red = [e for e in audit["errors"] if e["title"].startswith("Красное сальдо")]
    assert red and "20" in {r["Счет"] for r in red[0]["data"]}


def test_api_source_error_is_shown(monkeypatch):
    from api_client import OneCClient

    def boom(self, start, end):
        raise ValueError("OData вернул 401 Unauthorized")

    monkeypatch.setattr(OneCClient, "fetch_osv_monthly", boom)

    at = AppTest.from_file(APP, default_timeout=20)
    at.run()
    at.sidebar.radio(key="data_source").set_value("☁️ 1С:Фреш (OData)")
    at.run()
    at.sidebar.text_input(key="api_url").set_value("https://example.com/base")
    at.sidebar.text_input(key="api_user").set_value("u")
    at.sidebar.text_input(key="api_pass").set_value("p")
    at.sidebar.button(key="btn_fetch").click()
    at.run()

    assert not at.exception
    assert "api_balances" not in at.session_state
    assert any("401 Unauthorized" in e.value for e in at.sidebar.error)


def test_mock_data_still_works():
    at = AppTest.from_file(APP, default_timeout=20)
    at.run()
    assert not at.exception

    at.sidebar.button(key="btn_mock").click()
    at.run()
    assert not at.exception
    assert "mock_data" in at.session_state

    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception
    assert "audit" in at.session_state
