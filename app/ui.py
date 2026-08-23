import os
import sys
import traceback
import uuid
from datetime import date, datetime

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

import streamlit as st

from core.api_client import OneCClient
from core.auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    normalize_balances,
    normalize_documents,
)
from core.db import save_audit_log, load_audit_history
from core.comparator import compare_audits
from core.dashboard import accounts_list, block_dfs, build_dashboard_df
from core.loaders import load_osv_file

# Значения по умолчанию для подключения к 1С:Фреш (публичная демо-база).
# Переопределяются переменными окружения ONEC_URL / ONEC_USER / ONEC_PASS.
DEFAULT_ONEC_URL = os.environ.get("ONEC_URL", "https://msk1.1cfresh.com/a/ea/3418453")
DEFAULT_ONEC_USER = os.environ.get("ONEC_USER", "odata.user")

st.set_page_config(page_title="ИИ-Аудитор 1С", layout="wide")

st.title("ИИ-Аудитор 1С")

st.markdown(
    "Прототип: 5 контрольных проверок по ТЗ + ML-проверки. Данные — загруженный "
    "файл ОСВ (**CSV, XLS, XLSX, HTML** — отчет 1С «Оборотно-сальдовая ведомость», "
    "сохраненный как Excel/HTML) или прямая выгрузка ОСВ из **1С:Фреш** по OData. "
    "MXL не поддерживается — сохраните отчет в 1С как Excel или HTML. Опционально "
    "загружается реестр документов для проверки расчетов с контрагентами."
)


def _filter_documents_by_period(
    documents: pd.DataFrame | None,
    selected_periods: list[str]
) -> pd.DataFrame | None:
    """
    Оставляет операции не позднее конца последнего выбранного периода
    """

    if documents is None or documents.empty:
        return documents

    valid_periods: list = []
    i: int = 0
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
    documents: pd.DataFrame | None,
    options: dict,
    db_name: str,
    source_info: dict,
) -> dict:
    """
    Локальный запуск аудита (без внешнего API-бэкенда)
    """

    filtered_balances = balances.copy()

    # Фильтрация по периодам
    if options.get("periods") is not None:
        filtered_balances = filtered_balances[
            filtered_balances["Период"].isin(options["periods"])
        ]

    doc_filtered: pd.DataFrame | None = None
    if documents is not None:
        opts_periods = options.get("periods") or []
        doc_filtered = _filter_documents_by_period(
            documents.copy(),
            opts_periods
        )

    # Фильтрация по режиму аудита (По счетам / По контрагенту)
    audit_mode = options.get("audit_mode", "Полный")
    if audit_mode == "По счетам" and options.get("target_accounts"):
        filtered_balances = filtered_balances[
            filtered_balances["Счет"].astype(str).isin(options["target_accounts"])
        ]
    elif audit_mode == "По контрагенту" and options.get("target_subcontos"):
        filtered_balances = filtered_balances[
            filtered_balances["Субконто"].astype(str).isin(options["target_subcontos"])
        ]
        if doc_filtered is not None and "Контрагент" in doc_filtered.columns:
            doc_filtered = doc_filtered[
                doc_filtered["Контрагент"].astype(str).isin(options["target_subcontos"])
            ]

    if filtered_balances.empty:
        raise ValueError("После применения фильтров (период/счета/контрагенты) не осталось данных для проверки.")

    meta: dict = {"organization": options.get("organization") or ""}

    real_periods: list = []
    i: int = 0
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

    errors_list: list = []
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
    """
    Полная таблица ML-дублей контрагентов из findings (колонки А/Б/Сходство)
    """

    auditor = result.get("auditor")
    if auditor is None:
        return pd.DataFrame()
    frames: list = []
    idx: int = 0
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
    """
    Один блок детальной панели дашборда: список счетов + таблица строк
    """

    empty = block_df is None or getattr(block_df, "empty", True)
    if empty:
        label = title
    else:
        label = f"{title} — счетов: {len(accounts_list(block_df))}"
    with st.expander(label, expanded=False):
        st.caption(caption)
        if empty:
            st.info("Не найдено.")
            return
        accounts = accounts_list(block_df)
        st.markdown("**Счета:** " + ", ".join(accounts))
        st.dataframe(block_df, width="stretch", hide_index=True)


def _render_dashboard_exports(result: dict) -> None:
    """
    Кнопки выгрузки Excel/PDF для выбранной базы
    """

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
        c_excel.error(f"Excel экспорт недоступен: {exc}")
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
        c_pdf.error(f"PDF недоступен: {exc}")


def find_result_safe(
    history: list[dict],
    target_base: str,
    target_period: str | None = None
) -> dict | None:
    """
    Безопасный поиск результата аудита по Базе и Периоду без использования for
    """

    i: int = 0
    length: int = len(history)

    while i < length:
        res = history[i]

        # Проверяем совпадение по имени базы
        if res.get("db_name") == target_base:

            # Если период передан и он не пустой/прочерк,
            # проверяем точное совпадение
            if target_period is not None and target_period != "—":
                res_period = str(res.get("period", ""))

                if res_period == target_period:
                    return res
            else:
                # Если период не важен (режим "За период в целом"),
                # возвращаем первое совпадение по базе
                return res

        i += 1

    return None


def _render_dashboard(history: list[dict]) -> None:
    """
    Сводный дашборд по базам (Master-Detail)
    """

    if not history:
        return

    st.markdown("---")

    col_title, col_reset = st.columns([4, 1])
    col_title.header("📊 Сводный дашборд по базам")
    if col_reset.button(
        "✖️ Сбросить результаты",
        key="btn_reset_dash",
        use_container_width=True
    ):
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

    # Пытаемся извлечь период, если колонка есть в дэшбоарде
    selected_period = None
    if "Период" in dash.columns:
        selected_period = str(dash.iloc[row_idx]["Период"])

    result = find_result_safe(history, selected_base, selected_period)
    if result is None:
        st.info("Для выбранной базы нет детального результата аудита")
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
    # Счета берутся из accounts_with_errors (разбивает составные ячейки «60.01,
    # 60.02»), а не из сырых значений колонки «Счет».
    accounts = (
        auditor.accounts_with_errors()
        if auditor is not None
        else accounts_list(details)
    )
    if not accounts:
        st.info("По этой базе нет строк нарушений по счетам.")
        return

    idx: int = 0
    while idx < len(accounts):
        acc = accounts[idx]
        acc_rows = (
            auditor.account_report_df(acc)
            if auditor is not None
            else details[details["Счет"].astype(str) == acc]
        )
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

osv_files: list = []
fetch_api = False
api_url = ""
api_user = ""
api_pass = ""
api_start = date.today()
api_end = date.today()

if data_source.startswith("📁"):
    osv_files = st.sidebar.file_uploader(
        "ОСВ (CSV / XLS / XLSX / HTML)",
        type=["csv", "xls", "xlsx", "html", "htm"],
        accept_multiple_files=True,
        key="osv",
    )
    merge_mode = st.sidebar.radio(
        "Режим нескольких файлов",
        ["Объединить в одну базу (один аудит)", "Проверить каждый отдельно"],
        help="Объединить: если загружаете разные месяцы одной компании." \
            "Отдельно: если это разные базы/организации."
    )
else:
    # Источник 1С:Фреш всегда один аудит — режим нескольких файлов не применяется
    merge_mode = "Объединить в одну базу"
    with st.sidebar.expander("🔑 Доступ к 1С:Фреш", expanded=True):
        api_url = st.text_input(
            "URL базы",
            value=DEFAULT_ONEC_URL,
            key="api_url",
        )
        api_user = st.text_input(
            "Пользователь",
            value=DEFAULT_ONEC_USER,
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

with st.sidebar.expander("ML-проверки"):
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
balances: pd.DataFrame | None = None
documents: pd.DataFrame | None = None
source_info: dict = {}

# Список баз для аудита: [{"name": str, "df": pd.DataFrame, "info": dict}]
datasets_to_process = []

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
        datasets_to_process.append(
            {
                "name": "Тестовая база",
                "df": balances,
                "info": {}
            }
        )

    elif data_source.startswith("☁️") and "api_balances" in st.session_state:
        balances = st.session_state["api_balances"]
        documents = None
        source_info = st.session_state.get("api_meta", {})
        db_name = st.session_state.get("api_db_name", api_url.strip())
        datasets_to_process.append(
            {
                "name": db_name,
                "df": balances,
                "info": source_info
            }
        )

    elif data_source.startswith("📁") and osv_files:
        if merge_mode == "Объединить в одну базу":
            all_b: list = []
            for f in osv_files:
                d_df, info = load_osv_file(
                    f.name,
                    f.getvalue(),
                    plan_override=plan_input
                )
                all_b.append(d_df)
                if not source_info:
                    source_info = info

            balances = pd.concat(all_b, ignore_index=True).drop_duplicates()
            db_name = " + ".join([f.name for f in osv_files])
            datasets_to_process.append(
                {
                    "name": db_name,
                    "df": balances,
                    "info": source_info
                }
            )
        else: # Режим каждый отдельно
            for f in osv_files:
                b_df, info = load_osv_file(
                    f.name,
                    f.getvalue(),
                    plan_override=plan_input
                )
                datasets_to_process.append(
                    {
                        "name": f.name,
                        "df": b_df,
                        "info": info
                    }
                )

            if datasets_to_process:
                # Для предпросмотра на экране берем первый файл
                balances = datasets_to_process[0]["df"]
                source_info = datasets_to_process[0]["info"]

    elif "mock_data" in st.session_state:
        balances = st.session_state["mock_data"]["balances"]
        documents = st.session_state["mock_data"]["documents"]
        if not datasets_to_process:
            datasets_to_process.append(
                {
                    "name": "Тестовая база",
                    "df": balances,
                    "info": {}
                }
            )

except (ValueError, OSError) as exc:
    st.sidebar.error(str(exc))
    st.stop()

if not datasets_to_process:
    if data_source.startswith("☁️"):
        st.info("👈 Введите доступ к 1С:Фреш и нажмите «📡 Загрузить ОСВ из 1С» в панели слева.")
    else:
        st.info("👈 Загрузите файл(ы) ОСВ (CSV/XLS/XLSX/HTML) или нажмите «Использовать тестовые данные» в панели слева.")
    st.stop()

if source_info.get("title") or source_info.get("organization"):
    st.sidebar.caption(
        f"📄 Источник: {source_info.get('title') or ''}"
        f"{(' | Орг.: ' + source_info['organization']) if source_info.get('organization') else ''}"
    )

# Подготовка списков для фильтров
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
    "Группировка периодов",
    ["🗓 За период в целом", "📅 По месяцам"],
    key="period_mode"
)

st.sidebar.markdown("---")

# ============ Режим аудита ============
audit_mode = st.sidebar.radio(
    "Режим аудита",
    ["Полный", "По счетам", "По контрагенту"],
    key="audit_mode"
)

# Собираем уникальные счета для селектора
unique_accs_raw = balances["Счет"].dropna().unique().tolist()
unique_accs = []
i = 0
while i < len(unique_accs_raw):
    val = str(unique_accs_raw[i]).strip()
    if val:
        unique_accs.append(val)
    i += 1
unique_accs = sorted(unique_accs)

# Собираем уникальные субконто для селектора
unique_subs = []
if "Субконто" in balances.columns:
    unique_subs_raw = balances["Субконто"].dropna().unique().tolist()
    i = 0
    while i < len(unique_subs_raw):
        val = str(unique_subs_raw[i]).strip()
        if val and val != "-":
            unique_subs.append(val)
        i += 1
    unique_subs = sorted(unique_subs)

target_accounts = []
target_subcontos = []

if audit_mode == "По счетам":
    target_accounts = st.sidebar.multiselect("Выберите счета", options=unique_accs)
    if not target_accounts:
        st.sidebar.warning("⚠️ Выберите хотя бы один счет")
elif audit_mode == "По контрагенту":
    target_subcontos = st.sidebar.multiselect("Выберите контрагентов", options=unique_subs)
    if not target_subcontos:
        st.sidebar.warning("⚠️ Выберите хотя бы одного контрагента")
st.sidebar.markdown("---")
# =================================================

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

# Отображаем в UI предпросмотр уже с учетом выбранного режима аудита
if audit_mode == "По счетам" and target_accounts:
    filtered = filtered[filtered["Счет"].astype(str).isin(target_accounts)]
elif audit_mode == "По контрагенту" and target_subcontos:
    filtered = filtered[filtered["Субконто"].astype(str).isin(target_subcontos)]

st.dataframe(filtered, width="stretch", hide_index=True)

if filtered.empty:
    st.warning("Выбранные фильтры не содержат данных.")
    st.stop()

if documents is not None:
    doc_filtered = _filter_documents_by_period(documents, selected_periods)
    if doc_filtered is not None and audit_mode == "По контрагенту" and target_subcontos:
        doc_filtered = doc_filtered[doc_filtered["Контрагент"].astype(str).isin(target_subcontos)]

    doc_len = len(doc_filtered) if doc_filtered is not None else 0
    st.caption(
        f"📄 Загружен реестр документов: {len(documents)} операций "
        f"(после фильтрации осталось {doc_len})"
    )
else:
    doc_filtered = None

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
        "audit_mode": audit_mode,
        "target_accounts": target_accounts,
        "target_subcontos": target_subcontos,
    }

    try:
        with st.spinner("Анализируем данные..."):
            # Инициализируем историю, если она еще не загружена из БД
            history = st.session_state.setdefault(
                "audit_history",
                load_audit_history()
            )

            for ds in datasets_to_process:
                current_balances = ds["df"]
                current_db_name = ds["name"]
                current_info = ds["info"]

                real_periods = [p for p in selected_periods if p]
                if period_mode == "📅 По месяцам" and real_periods:
                    i = 0
                    while i < len(real_periods):
                        p = real_periods[i]
                        opt_copy = dict(options)
                        opt_copy["periods"] = [p]

                        res = _run_audit_local(
                            current_balances, doc_filtered,
                            opt_copy, current_db_name,
                            current_info
                        )
                        res["period"] = p
                        history.append(res)
                        # Сохраняем в БД
                        save_audit_log(res)
                        st.session_state["audit"] = res
                        i += 1
                else:
                    res = _run_audit_local(
                        current_balances, doc_filtered,
                        options, current_db_name,
                        current_info
                    )
                    history.append(res)
                    # Сохраняем в БД
                    save_audit_log(res)
                    st.session_state["audit"] = res

    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")

#                           ========== ВЫВОД РЕЗУЛЬТАТОВ И СРАВНЕНИЕ ==========
# При старте приложения подтягиваем историю из БД
if "audit_history" not in st.session_state:
    st.session_state["audit_history"] = load_audit_history()

history = st.session_state.get("audit_history", [])
if history:
    tab_dash, tab_compare = st.tabs([
        "Результаты проверок",
        "Динамика (Сравнение)"
    ])

    with tab_dash:
        _render_dashboard(history)

    with tab_compare:
        st.subheader("Сравнение двух проверков")

        if len(history) < 2:
            st.info(
                "Для сравнения проведите как минимум 2 аудита, " \
                "например загрузите базу до исправлений и после)"
            )
        else:
            audit_options: dict = {}
            for idx, h in enumerate(history):
                name = h.get("db_name", f"База {idx+1}")
                flags = h.get("total_flags", 0)
                date_str = h.get("viewed_at", "")
                label = f"{idx+1}. {name} (Ошибок: {flags}) - {date_str}"
                audit_options[label] = h

            c1, c2 = st.columns(2)
            with c1:
                old_label = st.selectbox(
                    "Базовая проверка (Было):",
                    list(audit_options.keys()),
                    index=0
                )
            with c2:
                new_label = st.selectbox(
                    "Новая проверка (Стало):",
                    list(audit_options.keys()),
                    index=len(audit_options)-1
                )

            if st.button("Сравнить базы", type="primary"):
                old_audit = audit_options[old_label]
                new_audit = audit_options[new_label]

                old_df = old_audit.get("details", pd.DataFrame())
                new_df = new_audit.get("details", pd.DataFrame())

                res = compare_audits(old_df, new_df)

                st.success(f"**Исправлено ошибок:** {len(res['resolved'])}")
                if not res["resolved"].empty:
                    st.dataframe(res["resolved"], width=True)

                st.error(f"⚠️ **Новые ошибки (появились):** {len(res['new'])}")
                if not res["new"].empty:
                    st.dataframe(res["new"], width=True)

                st.warning(f"⏳ **Остались без изменений:** {len(res['pending'])}")
                if not res["pending"].empty:
                    st.dataframe(res["pending"], width=True)
