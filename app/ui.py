import io
import os
import sys
import traceback
from datetime import date
from typing import Optional

import pandas as pd
import fpdf

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

import streamlit as st

from app import http_client
from core.api_client import OneCClient
from core.auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    RECOMMENDATIONS,
    normalize_balances,
    normalize_documents,
)
from core.loaders import load_osv_file

st.set_page_config(page_title="ИИ-Аудитор 1С", page_icon="", layout="wide")

st.title("🕵️‍♂️ ИИ-Аудитор 1С")

# Проверка работоспособности бэкенда
backend_ok = http_client.check_health()
if not backend_ok:
    st.warning(
        "🔌 Внимание: Бэкенд-сервер API (порт 8000) недоступен. "
        "Пожалуйста, запустите приложение через команду `python app/run.py`, "
        "чтобы включить полный функционал аудита и проваливания в отчеты."
    )

st.markdown(
    "Прототип: 5 контрольных проверок по ТЗ + ML-проверки. Данные — загруженный "
    "файл ОСВ (**CSV, XLS, XLSX, HTML** — отчет 1С «Оборотно-сальдовая ведомость», "
    "сохраненный как Excel/HTML) или прямая выгрузка ОСВ из **1С:Фреш** по OData. "
    "MXL не поддерживается — сохраните отчет в 1С как Excel или HTML. Опционально "
    "загружается реестр документов для проверки расчетов с контрагентами."
)


def load_csv(uploaded) -> pd.DataFrame:
    raw = uploaded.getvalue()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку файла")


def _filter_documents_by_period(
    documents: pd.DataFrame,
    selected_periods: list[str]
) -> pd.DataFrame:
    """
    Оставляет операции не позднее конца последнего выбранного периода
    """

    if documents is None or documents.empty:
        return documents
    ends = pd.to_datetime(
        pd.Series([p for p in selected_periods if p]),
        errors="coerce"
    ).dropna()
    if ends.empty:
        return documents
    return documents[documents["Дата"] <= ends.max()]


def _render_results(result: dict) -> None:
    """
    Отображает результаты аудита с фильтрами и поддерживает проваливание внутрь.
    """
    audit_id = result.get("audit_id")
    errors = result.get("errors", [])
    status_code = result.get("status")
    status_label = result.get("status_label")
    total_flags = result.get("total_flags", 0)
    db_name = result.get("db_name", "—")

    st.markdown("---")
    st.header("📋 Результаты проверки")

    reset = st.button("✖️ Сбросить результаты", key="btn_reset_results")
    if reset:
        if "audit" in st.session_state:
            del st.session_state["audit"]
        st.session_state.pop("summary_df", None)
        st.rerun()

    if status_code == "ok":
        st.success("🎉 Ошибок не найдено! Учет в порядке.")
        return

    st.error(f"🚨 Статус: {status_label}")

    details_list = result.get("details", [])
    details = pd.DataFrame(details_list) if details_list else pd.DataFrame()
    if details.empty:
        details = pd.DataFrame(columns=["Счет", "Субконто", "Проверка", "Период"])

    c1, c2 = st.columns(2)
    c1.metric("Красных флагов", total_flags)
    c2.metric("Проверок с находками", len(errors))

    available_checks = sorted(details["Проверка"].dropna().unique().tolist()) if not details.empty else []
    available_accounts = sorted(details["Счет"].astype(str).dropna().unique().tolist()) if not details.empty else []
    available_cps = sorted(details["Субконто"].astype(str).dropna().unique().tolist()) if not details.empty else []

    with st.expander(
        "🔎 Фильтры отображения (в Excel выгружается полный отчет)",
        expanded=False
    ):
        f1, f2, f3 = st.columns(3)
        sel_checks = f1.multiselect(
            "Проверки",
            options=available_checks,
            key="f_checks"
        )
        sel_accounts = f2.multiselect(
            "Счета",
            options=available_accounts,
            key="f_accounts"
        )
        sel_cps = f3.multiselect(
            "Контрагенты",
            options=available_cps,
            key="f_cps"
        )

    details_view = details
    if not details_view.empty:
        if sel_checks:
            details_view = details_view[details_view["Проверка"].isin(sel_checks)]
        if sel_accounts:
            details_view = details_view[details_view["Счет"].astype(str).isin(sel_accounts)]
        if sel_cps:
            details_view = details_view[details_view["Субконто"].astype(str).isin(sel_cps)]

    st.subheader("📋 Сводная ведомость")
    st.caption("💡 Кликните по строке счёта в таблице ниже, чтобы провалиться в детальный отчёт по этому счёту!")

    # Rebuild redesigned summary dynamically from filtered details
    from app.server import build_summary_view_api
    summary_view = build_summary_view_api(details_view, db_name)

    selected_account = None
    if summary_view.empty:
        st.info("По выбранным фильтрам строк нет.")
    else:
        # Use on_select="rerun" to enable click selection
        # key="summary_df" maps selection to st.session_state["summary_df"]
        st.dataframe(
            summary_view,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="summary_df"
        )

        # Determine if a row is selected
        summary_state = st.session_state.get("summary_df") or {}
        rows_selected = summary_state.get("selection", {}).get("rows", [])
        if rows_selected:
            row_idx = rows_selected[0]
            selected_account = str(summary_view.iloc[row_idx]["Счет"])

    # Detail view on demand if selected
    if selected_account and audit_id:
        st.markdown(f"### 🔎 Детализация по счёту {selected_account}")

        # Load detail on-demand from API
        with st.spinner(f"Загружаем детализацию по счету {selected_account}..."):
            try:
                detail_data = http_client.get_account_detail(audit_id, selected_account)
                by_period_df = pd.DataFrame(detail_data["by_period"])
                by_subconto_df = pd.DataFrame(detail_data["by_subconto"])

                # Render detail tabs
                tab1, tab2 = st.tabs(["📅 ОСВ по месяцам", "📊 Аналитика (Субконто)"])

                with tab1:
                    if by_period_df.empty:
                        st.info("Нет данных по периодам.")
                    else:
                        st.dataframe(by_period_df, width="stretch", hide_index=True)

                with tab2:
                    if by_subconto_df.empty:
                        st.info("Данные по аналитике (субконто) недоступны или отсутствуют.")
                        if db_name.startswith("http"):
                            st.caption("ℹ️ Движения / Карточка счета недоступны через OData на этой базе (ограничение OData 1С:Фреш).")
                    else:
                        st.dataframe(by_subconto_df, width="stretch", hide_index=True)

                if st.button("✖️ Закрыть детализацию", key="btn_close_detail"):
                    st.session_state.pop("summary_df", None)
                    st.rerun()
            except Exception as e:
                st.error(f"Не удалось загрузить детальный отчёт: {e}")

    st.subheader("🔍 Детальный отчет")
    if details_view.empty:
        st.info("Нет строк по выбранным фильтрам.")
    else:
        st.dataframe(details_view, width="stretch", hide_index=True)

    if audit_id:
        c_excel, c_pdf = st.columns(2)
        try:
            excel_data = http_client.get_excel_report(audit_id)
            c_excel.download_button(
                "💾 Выгрузить отчет в Excel",
                data=excel_data,
                file_name="audit_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="btn_download_excel"
            )
        except Exception as exc:
            c_excel.caption(f"Excel экспорт недоступен: {exc}")

        try:
            pdf_data = http_client.get_pdf_report(audit_id)
            c_pdf.download_button(
                "💾 Выгрузить отчет в PDF",
                data=pdf_data,
                file_name="audit_report.pdf",
                mime="application/pdf",
                key="btn_download_pdf"
            )
        except Exception as exc:
            c_pdf.caption(f"PDF недоступен: {exc}")

    st.markdown("---")
    st.subheader("🧾 Детализация по проверкам")
    for res in errors:
        if sel_checks and res["title"] not in sel_checks:
            continue
        data = pd.DataFrame(res["data"]) if res.get("data") else pd.DataFrame()
        if data.empty:
            continue
        if sel_accounts and "Счет" in data.columns:
            data = data[data["Счет"].astype(str).isin(sel_accounts)]
        if sel_cps and "Субконто" in data.columns:
            data = data[data["Субконто"].astype(str).isin(sel_cps)]
        if data.empty:
            continue
        icon = "🔴" if res["level"] == "error" else "🟡"
        with st.expander(
            f"{icon} {res['title']} — строк: {len(data)}, сумма: {res['amount']:,.2f}"):
            recommendation = RECOMMENDATIONS.get(res["title"], "")
            if recommendation:
                st.markdown(f"💡 **Рекомендация:** {recommendation}")
            st.dataframe(data, width="stretch", hide_index=True)


# ============ Боковая панель ============
st.sidebar.header("📥 Загрузка данных")
data_source = st.sidebar.radio(
    "Источник данных",
    ["📁 Файл (CSV/XLS/XLSX/HTML)", "☁️ 1С:Фреш (OData)"],
    key="data_source",
)
use_mock = st.sidebar.button("Использовать тестовые данные", key="btn_mock")

osv_file = None
fetch_api = False
api_url = api_user = api_pass = ""
api_start = api_end = date.today()

if data_source.startswith("📁"):
    osv_file = st.sidebar.file_uploader(
        "ОСВ (CSV / XLS / XLSX / HTML)",
        type=["csv", "xls", "xlsx", "html", "htm"],
        key="osv",
    )
else:
    with st.sidebar.expander("🔑 Доступ к 1С:Фреш", expanded=True):
        api_url = st.text_input(
            "URL базы",
            value=os.environ.get("ONEC_URL", "https://msk1.1cfresh.com/a/ea/3418453"),
            key="api_url",
        )
        api_user = st.text_input(
            "Пользователь",
            value=os.environ.get("ONEC_USER", "odata.user"),
            key="api_user",
        )
        api_pass = st.text_input(
            "Пароль",
            type="password",
            value=os.environ.get("ONEC_PASS", ""),
            key="api_pass",
        )
    c1, c2 = st.sidebar.columns(2)
    api_start = c1.date_input("Период с", value=date(date.today().year, 1, 1), key="api_start")
    api_end = c2.date_input("Период по", value=date.today(), key="api_end")
    fetch_api = st.sidebar.button("📡 Загрузить ОСВ из 1С", type="primary", key="btn_fetch")

with st.sidebar.expander("⚙️ Настройки проверок"):
    st.markdown("**Проверки ТЗ**")
    chk_red = st.checkbox("4.1 Красное сальдо", value=True)
    chk_expanded = st.checkbox("4.2 Развернутое сальдо", value=True)
    chk_unclosed = st.checkbox("4.3 Незакрытое сальдо на конец месяца", value=True)
    chk_000 = st.checkbox("4.4 Счет 000", value=True)
    chk_settlements = st.checkbox("4.5 Незакрытые расчеты с контрагентами", value=True)
    chk_group_balances = st.checkbox(
        "Контроль групп счетов (4.3)",
        value=False,
        help="Авансы, РБП, товары, денежные средства, кредиты: незакрытые остатки на конец периода.",
    )
    org_input = st.text_input(
        "Организация",
        value="",
        help="Название организации, подставляется в отчет (Excel/PDF)",
    )
    closing_input = st.text_input(
        "Закрываемые счета (через запятую)",
        value=", ".join(DEFAULT_CLOSING_ACCOUNTS),
        help="Счета, которые должны быть закрыты в конце месяца (проверка 4.3)",
    )
    plan_input = st.text_input(
        "План счетов: Тип (код:тип)",
        value="",
        placeholder="Например: 51:A, 60.01:AP",
        help="Дополнительные типы счетов для определения красного сальдо. Формат: Код:Тип, через запятую.",
    )

with st.sidebar.expander("🤖 ML-проверки"):
    ml_enabled = st.checkbox("Включить ML-проверки", value=True)
    ml_amount_anomalies = st.checkbox(
        "Нетипичные суммы операций",
        value=True,
        help="Поиск операций, чья сумма статистически выделяется по истории контрагента (медиана+MAD).",
    )
    ml_turnover_jumps = st.checkbox(
        "Скачки оборотов между периодами",
        value=True,
        help="Резкий рост/падение оборотов по счету между периодами (нужны данные за 2+ периода).",
    )
    ml_duplicates = st.checkbox(
        "Дубли контрагентов",
        value=True,
        help="Нечеткий поиск похожих названий: «ООО Ромашка» vs «Ромашка, ООО».",
    )
    dup_threshold = st.slider(
        "Порог сходства дублей, %",
        min_value=70, max_value=100, value=90,
    )
    anomaly_min_abs = st.number_input(
        "Порог аномалии суммы, ₽",
        min_value=0.0, value=1000.0, step=1000.0,
    )

# ============ Загрузка ============
balances: Optional[pd.DataFrame] = None
documents: Optional[pd.DataFrame] = None
source_info: dict = {}

if fetch_api:
    try:
        client = OneCClient(api_url.strip(), api_user.strip(), api_pass)
        start_s = api_start.strftime("%Y-%m-%dT00:00:00")
        end_s = api_end.strftime("%Y-%m-%dT23:59:59")
        with st.spinner("Загружаем ОСВ из 1С:Фреш..."):
            df = client.fetch_osv_monthly(start_s, end_s)
        st.session_state["api_balances"] = df
        st.session_state["api_meta"] = {
            "title": "ОСВ (1С:Фреш)",
            "period": f"{api_start} — {api_end}",
            "organization": "",
        }
        st.session_state["api_db_name"] = api_url.strip()
        for k in ("osv", "docs", "audit", "mock_data"):
            st.session_state.pop(k, None)
    except (ValueError, OSError) as exc:
        st.sidebar.error(str(exc))
        st.stop()
    except Exception as exc:
        st.error(f"Ошибка при загрузке данных из 1С: {exc}")
        st.code(traceback.format_exc(), language="python")
        st.stop()

try:
    if use_mock:
        st.session_state["mock_data"] = {
            "balances": normalize_balances(pd.read_csv(
                os.path.join(_DATA_DIR, "sample_data.csv"), dtype=str
            )),
            "documents": normalize_documents(pd.read_csv(
                os.path.join(_DATA_DIR, "sample_documents.csv"), dtype=str
            )),
        }
        for k in ("osv", "docs", "api_balances"):
            if k in st.session_state:
                del st.session_state[k]
        balances = st.session_state["mock_data"]["balances"]
        documents = st.session_state["mock_data"]["documents"]
    elif data_source.startswith("☁️") and "api_balances" in st.session_state:
        balances = st.session_state["api_balances"]
        documents = None
        source_info = st.session_state.get("api_meta", {})
    elif data_source.startswith("📁") and osv_file is not None:
        balances, source_info = load_osv_file(
            osv_file.name,
            osv_file.getvalue(),
            plan_override=plan_input
        )
    elif "mock_data" in st.session_state:
        balances = st.session_state["mock_data"]["balances"]
        documents = st.session_state["mock_data"]["documents"]
except (ValueError, OSError) as exc:
    st.sidebar.error(str(exc))
    st.stop()

if balances is None:
    if data_source.startswith("☁️"):
        st.info("👈 Введите доступ к 1С:Фреш и нажмите «📡 Загрузить ОСВ из 1С» в панели слева.")
    else:
        st.info("👈 Загрузите файл ОСВ (CSV/XLS/XLSX/HTML) или нажмите «Использовать тестовые данные» в панели слева.")
    st.stop()

if source_info.get("title") or source_info.get("organization"):
    st.sidebar.caption(
        f"📄 Источник: {source_info.get('title') or ''}"
        f"{(' | Орг.: ' + source_info['organization']) if source_info.get('organization') else ''}"
    )

periods = sorted(b for b in balances["Период"].dropna().unique() if b != "")
periods.insert(0, "")  # вариант «без периода» для старых форматов
selected_periods = st.sidebar.multiselect(
    "Период проверки",
    options=periods,
    default=periods,
    format_func=lambda p: p or "— без периода —"
)

closing_accounts = [a.strip() for a in closing_input.split(",") if a.strip()]

checks = {
    key
    for key, on in (
        ("red_balance", chk_red),
        ("expanded_balance", chk_expanded),
        ("unclosed_month_end", chk_unclosed),
        ("account_000", chk_000),
        ("settlements", chk_settlements),
    )
    if on
}

# ============ Исходные данные ============
st.subheader("📊 Исходные данные (Оборотно-сальдовая ведомость)")
mask = balances["Период"].isin(selected_periods)
filtered = balances[mask]
st.dataframe(filtered, width="stretch", hide_index=True)

if filtered.empty:
    st.warning("Выбранные периоды не содержат данных. Отмените фильтр по периодам.")
    st.stop()

if documents is not None:
    doc_filtered = _filter_documents_by_period(documents, selected_periods)
    st.caption(
        f"📄 Загружен реестр документов: {len(documents)} операций "
        f"(использовано {len(doc_filtered)} за выбранные периоды)"
    )
else:
    doc_filtered = None

# ============ Запуск проверки ============
if st.button("🚀 Запустить Аудит", type="primary", key="btn_audit"):
    options = {
        "checks": sorted(checks),
        "closing_accounts": closing_accounts,
        "plan_override": plan_input,
        "organization": org_input.strip(),
        "periods": selected_periods,
        "balance_group_checks": chk_group_balances,
        "ml_enabled": ml_enabled,
        "ml_amount_anomalies": ml_amount_anomalies,
        "ml_turnover_jumps": ml_turnover_jumps,
        "ml_duplicates": ml_duplicates,
        "dup_threshold": dup_threshold,
        "anomaly_min_abs": anomaly_min_abs,
    }
    try:
        if not backend_ok:
            raise http_client.ConnectionAPIError(
                "Сервер API (бэкенд) недоступен. Запустите приложение через `python app/run.py`."
            )
        with st.spinner("Анализируем данные через API-бэкенд..."):
            if use_mock:
                res = http_client.run_audit_mock(options)
            elif data_source.startswith("☁️"):
                res = http_client.run_audit_1c(
                    api_url,
                    api_user,
                    api_pass,
                    api_start.strftime("%Y-%m-%d"),
                    api_end.strftime("%Y-%m-%d"),
                    options,
                )
            elif osv_file is not None:
                res = http_client.run_audit_file(
                    osv_file.name,
                    osv_file.getvalue(),
                    None,
                    None,
                    options,
                )
            elif "mock_data" in st.session_state:
                res = http_client.run_audit_mock(options)
            else:
                res = None
        if res is not None:
            st.session_state["audit"] = res
            st.session_state.pop("summary_df", None)
    except (http_client.APIError, http_client.ConnectionAPIError) as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")

if "audit" in st.session_state:
    _render_results(st.session_state["audit"])
