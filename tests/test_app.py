import os

import pandas as pd
import pytest

from streamlit.testing.v1 import AppTest

APP = "app/ui.py"


@pytest.fixture(autouse=True)
def _mock_subconto_1c(monkeypatch):
    """Для 1С-источника подменяем OData-запросы (локального аудита это не касается).

    БД изолируется общей временной БД в conftest (AppTest работает in-process,
    поэтому AUDIT_DB_PATH задаётся до импорта core.db).
    """
    from core.api_client import OneCClient
    monkeypatch.setattr(
        OneCClient, "fetch_osv_monthly",
        lambda self, start, end: (
            pd.DataFrame([
                ["2026-01-31", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
                ["2026-02-28", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
            ], columns=[
                "Период", "Счет", "Субконто", "Тип",
                "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
                "КонецДебет", "КонецКредит",
            ]),
            {},
        ),
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


def test_logout_clears_session_and_returns_to_login(monkeypatch):
    """Кнопка «Выйти» очищает сессию и возвращает на экран входа."""
    from core import auth as auth_mod

    stored = auth_mod.hash_password("pw123")
    monkeypatch.setenv(auth_mod.AUDIT_USERS_ENV, f"tester:{stored}")

    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="login_user").set_value("tester")
    at.text_input(key="login_pass").set_value("pw123")
    at.button(key="btn_login").click()
    at.run()
    assert at.session_state["user"] == "tester"
    assert at.sidebar.button(key="btn_logout")

    at.sidebar.button(key="btn_logout").click()
    at.run()
    assert not at.exception
    assert "user" not in at.session_state
    # Вернулись на экран входа: форма логина есть, основной интерфейс скрыт.
    assert at.button(key="btn_login")
    assert not [b for b in at.button if b.key == "btn_audit"]


# ── Массовый аудит из JSON-списка баз ──

def test_batch_audit_loads_and_audits(tmp_path, monkeypatch):
    """Batch-режим загружает несколько баз из JSON и аудитует каждую."""
    from core.api_client import OneCClient

    # AppTest.from_file создаёт свежий контекст — монкейпатч на модуль не действует.
    # Пишем JSON в реальный корень, но БЕЗОПАСНО: бэкап/восстановление.
    real_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(real_root, "client_databases.json")

    backup: bytes | None = None
    if os.path.exists(json_path):
        with open(json_path, "rb") as f:
            backup = f.read()

    json_content = (
        '[{"name": "База А", "url": "https://a.1cfresh.com/x",'
        ' "login": "u1", "password": "p1"},'
        '{"name": "База Б", "url": "https://b.1cfresh.com/x",'
        ' "login": "u2", "password": "p2"},'
        '{"name": "База без пароля", "url": "https://c.1cfresh.com/x",'
        ' "login": "", "password": ""}]'
    )

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(json_content)

        fake_df = pd.DataFrame([
            ["2026-01-31", "20", "-", "A", 0.0, 100.0, 0.0, 0.0, 0.0, 100.0],
        ], columns=[
            "Период", "Счет", "Субконто", "Тип",
            "НачалоДебет", "НачалоКредит", "ОборотДебет", "ОборотКредит",
            "КонецДебет", "КонецКредит",
        ])

        def fake_fetch(self, start, end):
            return fake_df.copy(), {}

        monkeypatch.setattr(OneCClient, "fetch_osv_monthly", fake_fetch)

        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        assert not at.exception

        at.sidebar.radio(key="data_source").set_value("📊 Аудит всех баз")
        at.run()
        assert not at.exception

        caption_texts = [c.value for c in at.caption]
        assert any("3" in t for t in caption_texts)

        at.sidebar.button(key="btn_fetch_batch").click()
        at.run()
        assert not at.exception

        assert "batch_datasets" in at.session_state
        batch = at.session_state["batch_datasets"]
        assert len(batch) == 2
        assert batch[0]["name"] == "База А"
        assert batch[1]["name"] == "База Б"

        at.button(key="btn_audit").click()
        at.run()
        assert not at.exception

        history = at.session_state["audit_history"]
        names = [h["db_name"] for h in history]
        assert "База А" in names
        assert "База Б" in names
    finally:
        if backup is not None:
            with open(json_path, "wb") as f:
                f.write(backup)
        elif os.path.exists(json_path):
            os.remove(json_path)



