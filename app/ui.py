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
    ends = pd.to_datetime(
        pd.Series([p for p in selected_periods if p]),
        errors="coerce"
    ).dropna()
    if ends.empty:
        return documents
    return documents[documents["Дата"] <= ends.max()]


def build_summary_view(
    details_df: pd.DataFrame,
    db_name: str
) -> pd.DataFrame:
    """Сводная ведомость по счетам: Счет | Вид нарушений | Период(ы) | Дата просмотра."""
    if details_df is None or details_df.empty:
        return pd.DataFrame(columns=["Имя Базы", "Счет", "Вид нарушений", "Период(ы)", "Дата просмотра"])

    d = details_df.copy()
    d["Счет"] = d["Счет"].astype(str).fillna("")
    d["Период"] = d["Период"].astype(str).fillna("")
    d["Проверка"] = d["Проверка"].astype(str).fillna("")

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    by_account: dict[str, dict] = {}

    for _, row in d.iterrows():
        acc = row["Счет"]
        prov = row["Проверка"]
        per = row["Период"]
        if not acc:
            continue
        if acc not in by_account:
            by_account[acc] = {"violations": set(), "periods": set()}
        by_account[acc]["violations"].add(prov)
        if per:
            by_account[acc]["periods"].add(per)

    rows = []
    for acc in sorted(by_account.keys()):
        v_list = sorted(list(by_account[acc]["violations"]))
        p_list = sorted(list(by_account[acc]["periods"]))
        rows.append({
            "Имя Базы": db_name,
            "Счет": acc,
            "Вид нарушений": "; ".join(v_list) if v_list else "—",
            "Период(ы)": ", ".join(p_list) if p_list else "—",
            "Дата просмотра": now_str,
        })

    return pd.DataFrame(rows, columns=["Имя Базы", "Счет", "Вид нарушений", "Период(ы)", "Дата просмотра"])


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
            ends = pd.to_datetime(
                pd.Series([p for p in options["periods"] if p]),
                errors="coerce"
            ).dropna()
            if not ends.empty:
                doc_filtered = doc_filtered[doc_filtered["Дата"] <= ends.max()]

    meta = {"organization": options.get("organization") or ""}
    real_periods = [p for p in (options.get("periods") or []) if p]
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
    for err in auditor.errors:
        errors_list.append({
            "title": err["title"],
            "level": err["level"],
            "amount": err["amount"],
            "data": err["data"].to_dict(orient="records"),
        })

    return {
        "audit_id": str(uuid.uuid4()),
        "db_name": db_name,
        "status": report["status"],
        "status_label": report["status_label"],
        "total_flags": report["total_flags"],
        "details": report["details"],
        "errors": errors_list,
        "auditor": auditor,
        "balances_df": filtered_balances,
        "source": dict(source_info),
    }


def _load_account_detail(result: dict, account_code: str):
    """Детализация по счету: ОСВ по месяцам + аналитика (субконто)."""
    balances_df = result["balances_df"]
    df_acc = balances_df[balances_df["Счет"] == account_code]
    by_period = df_acc.sort_values("Период").copy()

    by_subconto = pd.DataFrame()
    source = result.get("source") or {}
    if source.get("url"):
        # Источник — 1С:Фреш (OData): тянем расшифровку по субконто
        try:
            client = OneCClient(
                str(source["url"]).strip(),
                str(source["user"]).strip(),
                str(source.get("password") or ""),
            )
            by_subconto = client.fetch_osv_account_subconto(
                source["start_s"], source["end_s"], account_code
            )
        except Exception:
            by_subconto = pd.DataFrame()
    else:
        # Файл / тестовые данные: строки с субконто могут лежать прямо в ОСВ
        df_sub = balances_df[
            (balances_df["Счет"] == account_code) & (balances_df["Субконто"] != "-")
        ]
        by_subconto = df_sub.copy()

    return by_period, by_subconto


def _render_results(result: dict) -> None:
    """Отображает результаты аудита с фильтрами и проваливанием внутрь."""
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

    details = result.get("details")
    if details is None or getattr(details, "empty", True):
        details = pd.DataFrame(columns=["Счет", "Субконто", "Проверка", "Период"])

    c1, c2 = st.columns(2)
    c1.metric("Красных флагов", total_flags)
    c2.metric("Проверок с находками", len(errors))

    available_checks = sorted(
        details["Проверка"].dropna().unique().tolist()
    ) if not details.empty else []
    available_accounts = sorted(
        details["Счет"].astype(str).dropna().unique().tolist()
    ) if not details.empty else []
    available_cps = sorted(
        details["Субконто"].astype(str).dropna().unique().tolist()
    ) if not details.empty else []

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

    summary_view = build_summary_view(details_view, db_name)

    selected_account = None
    if summary_view.empty:
        st.info("По выбранным фильтрам строк нет.")
    else:
        st.dataframe(
            summary_view,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="summary_df"
        )

        summary_state = st.session_state.get("summary_df") or {}
        rows_selected = summary_state.get("selection", {}).get("rows", [])
        if rows_selected:
            row_idx = rows_selected[0]
            selected_account = str(summary_view.iloc[row_idx]["Счет"])

    if selected_account and result.get("balances_df") is not None:
        st.markdown(f"### 🔎 Детализация по счёту {selected_account}")

        with st.spinner(f"Загружаем детализацию по счету {selected_account}..."):
            try:
                by_period_df, by_subconto_df = _load_account_detail(result, selected_account)

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
            except Exception as exc:
                st.error(f"Не удалось загрузить детальный отчёт: {exc}")

    st.subheader("🔍 Детальный отчет")
    if details_view.empty:
        st.info("Нет строк по выбранным фильтрам.")
    else:
        st.dataframe(details_view, width="stretch", hide_index=True)

    if result.get("auditor") is not None:
        c_excel, c_pdf = st.columns(2)
        try:
            excel_data = result["auditor"].to_excel()
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
            pdf_data = result["auditor"].to_pdf()
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
            res = _run_audit_local(balances, doc_filtered, options, db_name, source_info)
        st.session_state["audit"] = res
        st.session_state.pop("summary_df", None)
    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")

if "audit" in st.session_state:
    _render_results(st.session_state["audit"])
