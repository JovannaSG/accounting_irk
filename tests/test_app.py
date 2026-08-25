import pandas as pd
import pytest

from streamlit.testing.v1 import AppTest

APP = "app/ui.py"


@pytest.fixture(autouse=True)
def _mock_subconto_1c(monkeypatch):
    """Для 1С-источника подменяем OData-запросы (локального аудита это не касается)."""
    from core.api_client import OneCClient
    monkeypatch.setattr(
        OneCClient, "fetch_osv_monthly",
        lambda self, start, end: pd.DataFrame([
            ["2026-01-31", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
            ["2026-02-28", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
        ], columns=[
            "Период", "Счет", "Субконто", "Тип",
            "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
            "КонецДебет", "КонецКредит",
        ]),
    )
    monkeypatch.setattr(
        OneCClient, "fetch_osv_account_subconto",
        lambda self, start, end, account: pd.DataFrame(),
    )
    yield


def test_mock_audit_works():
    at = AppTest.from_file(APP, default_timeout=30)
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
    audit = at.session_state["audit"]
    assert audit["db_name"] == "Тестовая база"
    assert audit["status"] in ("ok", "warning", "error")
    assert audit["status_label"]


def test_mock_audit_results_rendered():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    audit = at.session_state["audit"]
    assert audit["status"] == "warning"
    assert audit["total_flags"] >= 1

    headers = [h.value for h in at.header]
    assert any("Сводный дашборд" in h for h in headers)


def test_drill_down_after_mock_audit():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    audit = at.session_state["audit"]
    details_df = audit["details"]
    if details_df.empty:
        pytest.skip("В тестовых данных нет нарушений для drill-down")

    dash_els = [d for d in at.dataframe if d.key == "dashboard_df"]
    assert dash_els, "мастер-таблица дашборда не отрисована"
    master = dash_els[0].value
    assert not master.empty

    at.session_state["dashboard_df"] = {"selection": {"rows": [0]}}
    at.run()
    assert not at.exception

    expands = [e.label for e in at.expander]
    assert any("Счёт" in e for e in expands)


def test_api_source_loads_osv_and_runs_audit():
    at = AppTest.from_file(APP, default_timeout=30)
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


def test_api_source_error_is_shown(monkeypatch):
    from core.api_client import OneCClient

    def boom(self, start, end):
        raise ValueError("OData вернул 401 Unauthorized")

    monkeypatch.setattr(OneCClient, "fetch_osv_monthly", boom)

    at = AppTest.from_file(APP, default_timeout=30)
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


def test_account_report_detail_rendered():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    audit = at.session_state["audit"]
    auditor = audit["auditor"]
    accounts = auditor.accounts_with_errors()
    if not accounts:
        pytest.skip("В тестовых данных нет счетов с нарушениями")

    at.session_state["dashboard_df"] = {"selection": {"rows": [0]}}
    at.run()
    assert not at.exception

    expands = [e.label for e in at.expander]
    assert any("Счёт" in e for e in expands)


def test_no_data_message_when_source_empty():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception

    info_texts = [i.value for i in at.info]
    assert any("тестовые данные" in t.lower() for t in info_texts)
    assert "audit" not in at.session_state


# ── Ограничение доступа (ТЗ §11): гейт активен только при AUDIT_USERS ──

def test_login_gate_blocks_wrong_password(monkeypatch):
    from core import auth as auth_mod

    stored = auth_mod.hash_password("pw123")
    monkeypatch.setenv(auth_mod.AUDIT_USERS_ENV, f"tester:{stored}")

    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    # Основной интерфейс не отрисован до входа
    assert not [b for b in at.button if b.key == "btn_audit"]

    at.text_input(key="login_user").set_value("tester")
    at.text_input(key="login_pass").set_value("wrong")
    at.button(key="btn_login").click()
    at.run()
    assert any("Неверный логин или пароль" in e.value for e in at.error)
    assert "user" not in at.session_state


def test_login_gate_passes_then_audits_with_user(monkeypatch):
    from core import auth as auth_mod

    stored = auth_mod.hash_password("pw123")
    monkeypatch.setenv(auth_mod.AUDIT_USERS_ENV, f"tester:{stored}")

    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="login_user").set_value("Tester")
    at.text_input(key="login_pass").set_value("pw123")
    at.button(key="btn_login").click()
    at.run()
    assert not at.exception
    assert at.session_state["user"] == "Tester"

    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception
    audit = at.session_state["audit"]
    assert audit["user"] == "Tester"
    # Версия логики проверок пишется в meta каждого прогона
    assert audit["meta"]["audit_logic_version"]
    assert audit["meta"]["organization"] == ""



