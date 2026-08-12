import os
import sys
import traceback
import uuid
from datetime import date, datetime
from typing import Optional

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

import streamlit as st

from core.api_client import OneCClient
from core.dashboard import accounts_list, block_dfs, build_dashboard_df, find_result
from core.auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    RECOMMENDATIONS,
    normalize_balances,
    normalize_documents,
)
from core.loaders import load_osv_file

st.set_page_config(page_title="ИИ-Аудитор 1С", page_icon="", layout="wide")

st.title("ИИ-Аудитор 1С")

st.markdown(
    "Прототип: 5 контрольных проверок по ТЗ + ML-проверки. Данные — загруженный "
    "файл ОСВ (**CSV, XLS, XLSX, HTML** — отчет 1С «Оборотно-сальдовая ведомость», "
    "сохраненный как Excel/HTML) или прямая выгрузка ОСВ из **1С:Фреш** по OData. "
    "MXL не поддерживается — сохраните отчет в 1С как Excel или HTML. Опционально "
    "загружается реестр документов для проверки расчетов с контрагентами."
)


def _filter_documents_by_period(
    documents: pd.DataFrame,
    selected_periods: list[str]
) -> pd.DataFrame:
    """Оставляет операции не позднее конца последнего выбранного периода."""
    if documents is None or documents.empty:
        return documents
    
    valid_periods = []
    i = 0
    while i < len(selected_periods):
        if selected_periods[i]:
            valid_periods.append(selected_periods[i])
        i += 1
        
    ends = pd.to_datetime(
        pd.Series(valid_periods),
        errors="coerce"
    ).dropna()
    
    if ends.empty:
        return documents
    return documents[documents["Дата"] <= ends.max()]


def _run_audit_local(
    balances: pd.DataFrame,
    documents: Optional[pd.DataFrame],
    options: dict,
    db_name: str,
    source_info: dict,
) -> dict:
    """Локальный запуск аудита (без внешнего API-бэкенда)."""
    filtered_balances = balances.copy()
    if options.get("periods") is not None:
        filtered_balances = filtered_balances[
            filtered_balances["Период"].isin(options["periods"])
        ]
    if filtered_balances.empty:
        raise ValueError("Выбранные периоды не содержат данных.")

    doc_filtered = None
    if documents is not None:
        doc_filtered = documents.copy()
        if options.get("periods"):
            valid_periods = []
            i = 0
            opts_periods = options["periods"]
            while i < len(opts_periods):
                if opts_periods[i]:
                    valid_periods.append(opts_periods[i])
                i += 1
                
            ends = pd.to_datetime(
                pd.Series(valid_periods),
                errors="coerce"
            ).dropna()
            if not ends.empty:
                doc_filtered = doc_filtered[doc_filtered["Дата"] <= ends.max()]

    meta = {"organization": options.get("organization") or ""}
    
    real_periods = []
    i = 0
    opts_periods_check = options.get("periods") or []
    while i < len(opts_periods_check):
        if opts_periods_check[i]:
            real_periods.append(opts_periods_check[i])
        i += 1
        
    if real_periods:
        meta["period"] = ", ".join(real_periods)
    if source_info.get("title"):
        meta["title"] = source_info["title"]

    auditor = AutoAuditor1C(
        filtered_balances,
        doc_filtered,
        closing_accounts=options["closing_accounts"],
        checks=set(options["checks"]),
        meta=meta,
        balance_group_checks=options["balance_group_checks"],
        ml_enabled=options["ml_enabled"],
        ml_amount_anomalies=options["ml_amount_anomalies"],
        ml_turnover_jumps=options["ml_turnover_jumps"],
        ml_duplicates=options["ml_duplicates"],
        dup_threshold=options["dup_threshold"],
        anomaly_min_abs=options["anomaly_min_abs"],
    )
    auditor.run_audit()
    report = auditor.report()

    errors_list = []
    i = 0
    while i < len(auditor.errors):
        err = auditor.errors[i]
        errors_list.append({
            "title": err["title"],
            "level": err["level"],
            "amount": err["amount"],
            "data": err["data"].to_dict(orient="records"),
        })
        i += 1

    return {
        "audit_id": str(uuid.uuid4()),
        "db_name": db_name,
        "accountant": options.get("accountant") or "",
        "viewed_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "status": report["status"],
        "status_label": report["status_label"],
        "total_flags": report["total_flags"],
        "details": report["details"],
        "errors": errors_list,
        "auditor": auditor,
        "balances_df": filtered_balances,
        "source": dict(source_info),
    }


def _dashboard_duplicates_df(result: dict) -> pd.DataFrame:
    """Полная таблица ML-дублей контрагентов из findings (колонки А/Б/Сходство)."""
    auditor = result.get("auditor")
    if auditor is None:
        return pd.DataFrame()
    frames = []
    idx = 0
    while idx < len(auditor.errors):
        err = auditor.errors[idx]
        if str(err["title"]).startswith("ML: возможные дубли контрагентов"):
            data = err["data"]
            if data is not None and not getattr(data, "empty", True):
                frames.append(data.copy())
        idx += 1
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _render_dashboard_block(title: str, caption: str, block_df) -> None:
    """Один блок детальной панели дашборда: список счетов + таблица строк."""
    empty = block_df is None or getattr(block_df, "empty", True)
    label = title if empty else f"{title} — счетов: {len(accounts_list(block_df))}"
    with st.expander(label, expanded=False):
        st.caption(caption)
        if empty:
            st.info("Не найдено.")
            return
        accounts = accounts_list(block_df)
        st.markdown("**Счета:** " + ", ".join(accounts))
        st.dataframe(block_df, width="stretch", hide_index=True)


def _render_dashboard_exports(result: dict) -> None:
    """Кнопки выгрузки Excel/PDF для выбранной базы."""
    auditor = result.get("auditor")
    if auditor is None:
        return
        
    c_excel, c_pdf = st.columns(2)
    try:
        excel_data = auditor.to_excel()
        c_excel.download_button(
            "💾 Выгрузить отчет в Excel",
            data=excel_data,
            file_name="audit_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dash_btn_download_excel",
        )
    except Exception as exc:
        c_excel.caption(f"Excel экспорт недоступен: {exc}")
    try:
        pdf_data = auditor.to_pdf()
        c_pdf.download_button(
            "💾 Выгрузить отчет в PDF",
            data=pdf_data,
            file_name="audit_report.pdf",
            mime="application/pdf",
            key="dash_btn_download_pdf",
        )
    except Exception as exc:
        c_pdf.caption(f"PDF недоступен: {exc}")


def _render_dashboard(history: list[dict]) -> None:
    """Сводный дашборд по базам (Master-Detail)."""
    if not history:
        return

    st.markdown("---")
    
    col_title, col_reset = st.columns([4, 1])
    col_title.header("📊 Сводный дашборд по базам")
    if col_reset.button("✖️ Сбросить результаты", key="btn_reset_dash", use_container_width=True):
        keys_to_del = ["audit", "audit_history", "dashboard_df"]
        i = 0
        while i < len(keys_to_del):
            st.session_state.pop(keys_to_del[i], None)
            i += 1
        st.rerun()
        
    st.caption(
        "Мастер-вид: кликните по строке базы, чтобы увидеть ошибки по счетам "
        "этой базы в панели ниже."
    )

    dash = build_dashboard_df(history)
    if dash.empty:
        st.info("Пока нет результатов аудита для сводного дашборда.")
        return

    st.dataframe(
        dash,
        on_select="rerun",
        selection_mode="single-row",
        hide_index=True,
        use_container_width=True,
        key="dashboard_df",
    )

    st.markdown("---")
    st.subheader("📋 Детализация по счетам")

    dash_state = st.session_state.get("dashboard_df") or {}
    rows_selected = dash_state.get("selection", {}).get("rows", [])
    if not rows_selected:
        st.info("Выберите базу в таблице выше для просмотра ошибок по счетам")
        return

    row_idx = rows_selected[0]
    selected_base = str(dash.iloc[row_idx]["База"])

    result = find_result(history, selected_base)
    if result is None:
        st.info("Для выбранной базы нет детального результата аудита.")
        return

    auditor = result.get("auditor")
    details = result.get("details")
    if details is None or getattr(details, "empty", True):
        st.info("По этой базе нарушений не найдено.")
        return

    st.markdown(f"### 🗄️ База: {selected_base}")
    m1, m2, m3 = st.columns(3)
    m1.metric("Бухгалтер", result.get("accountant") or "—")
    m2.metric("Дата просмотра", result.get("viewed_at") or "—")
    m3.metric("Красных флагов", result.get("total_flags", 0))

    _render_dashboard_exports(result)

    blocks = block_dfs(details)
    _render_dashboard_block(
        "🔴 Блок 1. Красное сальдо (отрицательные остатки)",
        "Проверка 4.1 по ТЗ: отрицательный остаток по активному счёту "
        "(или дебетовый по пассивному).",
        blocks.get("red"),
    )
    _render_dashboard_block(
        "🟡 Блок 2. Незакрытые счета (на конец месяца)",
        "Проверки 4.3–4.4 по ТЗ: счета, не закрытые на конец месяца, "
        "зависшее сальдо между периодами и счёт 000.",
        blocks.get("unclosed"),
    )
    _render_dashboard_block(
        "🟠 Блок 3. Развёрнутое сальдо",
        "Проверка 4.2 по ТЗ: одновременно дебиторка и кредиторка по одной "
        "аналитике/контрагенту.",
        blocks.get("expanded"),
    )

    dups = _dashboard_duplicates_df(result)
    with st.expander("🔎 ML-дубли контрагентов (поиск дублей)"):
        st.caption(
            "ML-поиск похожих названий контрагентов («ООО Ромашка» vs "
            "«Ромашка, ООО»), которые могут «раздвоить» взаиморасчеты."
        )
        if dups.empty:
            st.caption("Дублей контрагентов не обнаружено.")
        else:
            st.dataframe(dups, width="stretch", hide_index=True)

    st.markdown("---")
    st.subheader("📄 Детализация по счетам")
    accounts = accounts_list(details)
    if not accounts:
        st.info("По этой базе нет строк нарушений по счетам.")
        return

    idx = 0
    while idx < len(accounts):
        acc = accounts[idx]
        acc_rows = details[details["Счет"].astype(str) == acc]
        subconto = auditor.account_subconto(acc) if auditor is not None else []
        acc_dups = (
            auditor.account_subconto_duplicates(acc)
            if auditor is not None
            else pd.DataFrame()
        )
        with st.expander(f"📄 Счёт {acc} — нарушений: {len(acc_rows)}"):
            st.dataframe(acc_rows, width="stretch", hide_index=True)
            if subconto:
                st.markdown(f"**Субконто / контрагенты по счёту {acc}:**")
                st.dataframe(
                    pd.DataFrame({"Субконто / Контрагент": subconto}),
                    width="stretch",
                    hide_index=True,
                )
            if acc_dups is not None and not acc_dups.empty:
                st.markdown(f"**Возможные дубли контрагентов по счёту {acc}:**")
                st.dataframe(acc_dups, width="stretch", hide_index=True)
        idx += 1


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
api_url = ""
api_user = ""
api_pass = ""
api_start = date.today()
api_end = date.today()

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
    accountant_input = st.text_input(
        "Бухгалтер",
        value="",
        help="ФИО бухгалтера базы — отображается в сводном дашборде.",
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
            "url": api_url.strip(),
            "user": api_user.strip(),
            "password": api_pass,
            "start_s": start_s,
            "end_s": end_s,
        }
        st.session_state["api_db_name"] = api_url.strip()
        
        keys_to_del = ["osv", "docs", "audit", "mock_data"]
        i = 0
        while i < len(keys_to_del):
            st.session_state.pop(keys_to_del[i], None)
            i += 1
            
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
        keys_to_del = ["osv", "docs", "api_balances"]
        i = 0
        while i < len(keys_to_del):
            if keys_to_del[i] in st.session_state:
                del st.session_state[keys_to_del[i]]
            i += 1
            
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

unique_periods = balances["Период"].dropna().unique().tolist()
periods = []
i = 0
while i < len(unique_periods):
    if unique_periods[i] != "":
        periods.append(unique_periods[i])
    i += 1
periods = sorted(periods)
periods.insert(0, "")

selected_periods = st.sidebar.multiselect(
    "Период проверки",
    options=periods,
    default=periods,
    format_func=lambda p: p or "— без периода —"
)

period_mode = st.sidebar.radio(
    "Режим периода", 
    ["🗓 За период в целом", "📅 По месяцам"], 
    key="period_mode"
)

closing_parts = closing_input.split(",")
closing_accounts = []
i = 0
while i < len(closing_parts):
    val = closing_parts[i].strip()
    if val:
        closing_accounts.append(val)
    i += 1

checks_data = [
    ("red_balance", chk_red),
    ("expanded_balance", chk_expanded),
    ("unclosed_month_end", chk_unclosed),
    ("account_000", chk_000),
    ("settlements", chk_settlements)
]
checks = set()
i = 0
while i < len(checks_data):
    if checks_data[i][1]:
        checks.add(checks_data[i][0])
    i += 1

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
if data_source.startswith("☁️"):
    db_name = st.session_state.get("api_db_name", api_url.strip())
elif osv_file is not None:
    db_name = osv_file.name
else:
    db_name = "Тестовая база"

if st.button("🚀 Запустить Аудит", type="primary", key="btn_audit"):
    options = {
        "checks": sorted(checks),
        "closing_accounts": closing_accounts,
        "plan_override": plan_input,
        "organization": org_input.strip(),
        "accountant": accountant_input.strip(),
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
        with st.spinner("Анализируем данные..."):
            history = st.session_state.setdefault("audit_history", [])
            
            if period_mode == "📅 По месяцам" and selected_periods:
                i = 0
                while i < len(selected_periods):
                    p = selected_periods[i]
                    opt_copy = dict(options)
                    opt_copy["periods"] = [p] if p else []
                    
                    res = _run_audit_local(balances, doc_filtered, opt_copy, db_name, source_info)
                    res["period"] = p
                    history.append(res)
                    st.session_state["audit"] = res
                    i += 1
            else:
                res = _run_audit_local(balances, doc_filtered, options, db_name, source_info)
                history.append(res)
                st.session_state["audit"] = res

    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")

_render_dashboard(st.session_state.get("audit_history") or [])
