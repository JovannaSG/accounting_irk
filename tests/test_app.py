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
    assert any("Результаты проверки" in h for h in headers)


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

    summary_els = [d for d in at.dataframe if d.key == "summary_df"]
    assert summary_els, "сводная ведомость не отрисована"
    summary_view = summary_els[0].value
    assert not summary_view.empty

    account = str(summary_view.iloc[0]["Счет"])

    # Эмулируем клик по первой строке сводной ведомости
    at.session_state["summary_df"] = {"selection": {"rows": [0]}}
    at.run()
    assert not at.exception
    md_texts = [m.value for m in at.markdown]
    assert any(f"Детализация по счёту {account}" in t for t in md_texts)


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


def test_account_report_section_renders():
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

    sb = [s for s in at.selectbox if s.key == "account_report_select"]
    assert sb, "selectbox отчёта по счёту не отрисован"
    assert [str(o) for o in sb[0].options] == [f"Счёт {a}" for a in accounts]

    account = str(sb[0].value)
    md_texts = [m.value for m in at.markdown]
    assert any(f"Все нарушения счёта {account}" in t for t in md_texts)

    # переключение счёта обновляет заголовок таблицы нарушений
    other = next((o for o in accounts if str(o) != account), None)
    if other is not None:
        sb[0].set_value(other).run()
        assert not at.exception
        md_texts = [m.value for m in at.markdown]
        assert any(f"Все нарушения счёта {other}" in t for t in md_texts)


def test_account_report_subconto_search_renders():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    auditor = at.session_state["audit"]["auditor"]
    accounts = auditor.accounts_with_errors()
    with_subconto = [a for a in accounts if auditor.account_subconto(a)]
    if not with_subconto:
        pytest.skip("Нет счёта с субконто в тестовых данных")

    sb = [s for s in at.selectbox if s.key == "account_report_select"][0]
    sb.set_value(with_subconto[0]).run()
    assert not at.exception

    search = [t for t in at.text_input if t.key == "account_subconto_search"]
    assert search, "поле поиска субконто/контрагента не отрисовано"
    search[0].set_value("ром").run()
    assert not at.exception


def test_no_data_message_when_source_empty():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception

    info_texts = [i.value for i in at.info]
    assert any("тестовые данные" in t.lower() for t in info_texts)
    assert "audit" not in at.session_state


def test_account_pass_renders_for_api_source(monkeypatch):
    from core.api_client import OneCClient

    def pass_osv(self, start, end, account):
        assert account == "20"
        return pd.DataFrame([
            ["2026-01-31", "20", "ООО Ромашка", "A",
             0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
        ], columns=[
            "Период", "Счет", "Субконто", "Тип",
            "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
            "КонецДебет", "КонецКредит",
        ])

    monkeypatch.setattr(OneCClient, "fetch_osv_account_subconto_monthly", pass_osv)

    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio(key="data_source").set_value("☁️ 1С:Фреш (OData)")
    at.run()
    at.sidebar.text_input(key="api_url").set_value("https://example.com/base")
    at.sidebar.text_input(key="api_user").set_value("odata.user")
    at.sidebar.text_input(key="api_pass").set_value("secret")
    at.sidebar.button(key="btn_fetch").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    assert "account_pass" in at.session_state
    pass_data = at.session_state["account_pass"]
    assert pass_data is not None
    assert pass_data["audit_id"] == at.session_state["audit"]["audit_id"]
    assert "20" in {str(a) for a in pass_data["summary_df"]["Счет"]}

    subtitles = [s.value for s in at.subheader]
    assert any("Автопроход по счетам" in s for s in subtitles)

    expands = [e.label for e in at.expander]
    assert any("Счёт 20" in e for e in expands)

    details_df = pass_data["details_df"]
    assert not details_df.empty
    assert set(details_df["Счет"]) == {"20"}


def test_account_pass_handles_fetch_failure(monkeypatch):
    from core.api_client import OneCClient

    def boom(self, start, end, account):
        raise ValueError("OData вернул 401")

    monkeypatch.setattr(OneCClient, "fetch_osv_account_subconto_monthly", boom)

    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio(key="data_source").set_value("☁️ 1С:Фреш (OData)")
    at.run()
    at.sidebar.text_input(key="api_url").set_value("https://example.com/base")
    at.sidebar.text_input(key="api_user").set_value("u")
    at.sidebar.text_input(key="api_pass").set_value("p")
    at.sidebar.button(key="btn_fetch").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    assert "account_pass" in at.session_state
    pass_data = at.session_state["account_pass"]
    assert pass_data is not None
    row = pass_data["summary_df"].iloc[0]
    assert row["Ошибка"]
    assert row["Уровень"] == "error"

    warns = [w.value for w in at.warning]
    assert any("Не удалось получить индивидуальный отчёт" in w for w in warns)
