import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core.dashboard import (
    DASHBOARD_COLUMNS,
    accounts_list,
    block_dfs,
    build_dashboard_df,
    build_master_row,
    find_result,
)
import core.db

APP = "app/ui.py"


def details(rows):
    return pd.DataFrame(rows, columns=[
        "Счет", "Проверка", "Период", "Уровень", "Сумма",
    ])


def result(db="База 1", accountant="Иванова И.И.", rows=None):
    return {
        "db_name": db,
        "accountant": accountant,
        "viewed_at": "12.08.2026 10:00",
        "details": details(rows or []),
    }


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Изолирует БД для каждого UI теста"""
    test_db = tmp_path / "test_dashboard_history.db"
    monkeypatch.setattr(core.db, "_DB_PATH", str(test_db))


def test_master_row_red_balance():
    r = result(rows=[
        ["51", "Красное сальдо: активный счет с кредитовым остатком", "2026-01-31", "error", 5000.0],
    ])
    row = build_master_row(r)
    assert row["Бухгалтер"] == "Иванова И.И."
    assert row["База"] == "База 1"
    assert row["Дата просмотра"] == "12.08.2026 10:00"
    assert row["Сальдо красным, счет"] == "51"
    assert row["Развернутое сальдо, счет"] == "—"
    assert row["Не закрыт период, счет, период"] == "—"
    assert row["Не закрыты документами, счет"] == "—"


def test_master_row_expanded_balance():
    r = result(rows=[
        ["60.01", "Развернутое сальдо по аналитике", "2026-01-31", "warning", 100.0],
    ])
    row = build_master_row(r)
    assert row["Развернутое сальдо, счет"] == "60.01"


def test_master_row_unclosed_period_with_period():
    r = result(rows=[
        ["90.01", "Незакрытое сальдо на конец месяца (закрываемые счета)", "2026-01-31", "error", 0.0],
        ["20", "Зависшее сальдо (не меняется между периодами)", "2026-02-28", "warning", 0.0],
    ])
    row = build_master_row(r)
    values = set(row["Не закрыт период, счет, период"].split(", "))
    assert "90.01" in values and "2026-01-31" in values
    assert "20" in values and "2026-02-28" in values


def test_master_row_unclosed_documents():
    r = result(rows=[
        ["60.01", "Контрагенты: расчеты не закрыты документами", "2026-01-31", "error", 700.0],
    ])
    row = build_master_row(r)
    assert row["Не закрыты документами, счет"] == "60.01"


def test_master_row_ignores_ml_and_000_maps_to_unclosed():
    r = result(rows=[
        ["60.01", "ML: возможные дубли контрагентов", "2026-01-31", "warning", 0.0],
        ["000", "Незакрытое сальдо на счете 000", "2026-01-31", "error", 1.0],
    ])
    row = build_master_row(r)
    assert row["Сальдо красным, счет"] == "—"
    assert "000" in row["Не закрыт период, счет, период"]


def test_master_row_deduplicates_accounts():
    r = result(rows=[
        ["51", "Красное сальдо: активный счет с кредитовым остатком", "2026-01-31", "error", 1.0],
        ["51", "Красное сальдо: активный счет с кредитовым остатком", "2026-02-28", "error", 2.0],
    ])
    row = build_master_row(r)
    assert row["Сальдо красным, счет"] == "51"


def test_master_row_empty_details():
    row = build_master_row(result(rows=[]))
    assert row["Сальдо красным, счет"] == "—"
    assert row["Развернутое сальдо, счет"] == "—"
    assert row["Не закрыт период, счет, период"] == "—"
    assert row["Не закрыты документами, счет"] == "—"


def test_dashboard_df_columns_and_rows():
    rows_data = [
        ["51", "Красное сальдо: активный счет с кредитовым остатком", "2026-01-31", "error", 5.0],
        ["60.01", "Контрагенты: расчеты не закрыты документами", "2026-01-31", "error", 7.0],
    ]
    df = build_dashboard_df([
        result(db="База 1", rows=rows_data),
        result(db="База 2", accountant="Петров П.П.", rows=[]),
    ])

    assert list(df.columns) == DASHBOARD_COLUMNS
    assert list(df["База"]) == ["База 1", "База 2"]
    assert list(df["Бухгалтер"]) == ["Иванова И.И.", "Петров П.П."]
    assert df.iloc[0]["Сальдо красным, счет"] == "51"
    assert df.iloc[0]["Не закрыты документами, счет"] == "60.01"
    assert df.iloc[1]["Сальдо красным, счет"] == "—"


def test_dashboard_df_empty():
    df = build_dashboard_df([])
    assert df.empty
    assert list(df.columns) == DASHBOARD_COLUMNS


def test_find_result():
    history = [result(db="База 1"), result(db="База 2")]
    assert find_result(history, "База 2")["db_name"] == "База 2"
    assert find_result(history, "Нет такой базы") is None


def test_block_dfs_splits_by_check_type():
    d = details([
        ["51", "Красное сальдо: активный счет с кредитовым остатком", "2026-01-31", "error", 1.0],
        ["60.01", "Развернутое сальдо по аналитике", "2026-01-31", "warning", 2.0],
        ["90.01", "Незакрытое сальдо на конец месяца (закрываемые счета)", "2026-01-31", "error", 3.0],
        ["20", "Зависшее сальдо (не меняется между периодами)", "2026-02-28", "warning", 4.0],
        ["000", "Незакрытое сальдо на счете 000", "2026-01-31", "error", 5.0],
        ["60.01", "Контрагенты: расчеты не закрыты документами", "2026-01-31", "error", 6.0],
        ["60.01", "ML: возможные дубли контрагентов", "2026-01-31", "warning", 0.0],
    ])
    blocks = block_dfs(d)

    assert set(accounts_list(blocks["red"])) == {"51"}
    assert set(accounts_list(blocks["expanded"])) == {"60.01"}
    assert set(accounts_list(blocks["unclosed"])) == {"000", "20", "90.01"}

    # 4.5 и ML-дубли в блоки не попадают
    all_block_rows = (
        len(blocks["red"]) + len(blocks["expanded"]) + len(blocks["unclosed"])
    )
    assert all_block_rows == 5
    assert len(blocks["red"]["Счет"]) == 1
    assert len(blocks["expanded"]["Счет"]) == 1
    assert len(blocks["unclosed"]["Счет"]) == 3


def test_block_dfs_empty_details():
    blocks = block_dfs(pd.DataFrame(columns=["Счет", "Проверка", "Период"]))
    for block in ("red", "unclosed", "expanded"):
        assert blocks[block].empty


def test_block_dfs_missing_column():
    blocks = block_dfs(pd.DataFrame({"a": [1]}))
    for block in ("red", "unclosed", "expanded"):
        assert blocks[block].empty


def test_accounts_list_sorted_and_deduplicated():
    d = details([
        ["51", "x", "2026-01-31", "error", 1.0],
        ["60.01", "x", "2026-01-31", "error", 2.0],
        ["51", "x", "2026-02-28", "error", 3.0],
    ])
    assert accounts_list(d) == ["51", "60.01"]


def test_accounts_list_empty():
    assert accounts_list(pd.DataFrame()) == []
    assert accounts_list(pd.DataFrame({"Счет": [None, "", "  "]}, dtype=object)) == []


def test_dashboard_renders_master_and_detail():
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    assert "audit_history" in at.session_state
    dash_els = [d for d in at.dataframe if d.key == "dashboard_df"]
    assert dash_els, "мастер-таблица дашборда не отрисована"
    master = dash_els[0].value
    assert list(master.columns) == DASHBOARD_COLUMNS
    assert len(master) == 1
    assert master.iloc[0]["База"] == "Тестовая база"

    headers = [h.value for h in at.header]
    assert any("Сводный дашборд" in h for h in headers)
    subtitles = [s.value for s in at.subheader]
    assert any("Детализация по счетам" in s for s in subtitles)

    # До выбора строки — подсказка
    info_texts = [i.value for i in at.info]
    assert any("Выберите базу в таблице выше" in t for t in info_texts)

    # Клик по строке -> детализация по счетам базы
    at.session_state["dashboard_df"] = {"selection": {"rows": [0]}}
    at.run()
    assert not at.exception

    expands = [e.label for e in at.expander]
    assert any("Счёт" in e for e in expands)
    assert any("База: Тестовая база" in m for m in (m.value for m in at.markdown))


def test_dashboard_detail_blocks_dups_and_exports(tmp_path, monkeypatch):
    # 1. Изолируем БД от других тестов, чтобы история была кристально чистой
    test_db = tmp_path / "test_dashboard_history.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(test_db))

    # 2. Запускаем приложение
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.button(key="btn_mock").click()
    at.run()
    at.button(key="btn_audit").click()
    at.run()
    assert not at.exception

    # 3. Имитируем клик по ПЕРВОЙ строке в таблице (индекс 0)
    at.session_state["dashboard_df"] = {
        "selection": {"rows": [0], "columns": []}
    }
    at.run()
    assert not at.exception

    # 4. Проверяем заголовок базы
    # Используем any() и in, чтобы тест не упал из-за лишних пробелов в Markdown
    markdowns = [m.value for m in at.markdown]
    assert any("### 🗄️ База: Тестовая база" in m for m in markdowns)

    # 5. Кнопки выгрузки Excel/PDF (Правильное API AppTest)
    # AppTest позволяет искать элементы по key напрямую!
    excel_btn = at.download_button(key="dash_btn_download_excel")
    assert excel_btn, "Кнопка Excel не найдена"
    pdf_btn = at.download_button(key="dash_btn_download_pdf")
    assert pdf_btn, "Кнопка PDF не найдена"

    # 6. Проверяем блоки (expander)
    expands = [e.label for e in at.expander]
    assert any("Блок 1. Красное сальдо" in e for e in expands)
    assert any("Блок 2. Незакрытые счета" in e for e in expands)
    assert any("Блок 3. Развёрнутое сальдо" in e for e in expands)
    assert any("ML-дубли контрагентов" in e for e in expands)

    # 7. Детализация по счетам (expander по каждому счёту)
    assert any("📄 Счёт" in e for e in expands)

    # 8. Проверка State
    assert "audit" in at.session_state
    audit = at.session_state["audit"]
    assert audit is not None
    assert "audit_id" in audit
