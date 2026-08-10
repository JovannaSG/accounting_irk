import io
import traceback
from typing import Optional

import pandas as pd
import streamlit as st

from auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    RECOMMENDATIONS,
    normalize_balances,
    normalize_documents,
)
from loaders import load_osv_file

st.set_page_config(page_title="ИИ-Аудитор 1С", page_icon="🕵️‍♂️", layout="wide")

try:
    import fpdf  # noqa: F401
    pdf_available = True
except ImportError:
    pdf_available = False

st.title("🕵️‍♂️ ИИ-Аудитор: Прототип (без 1С)")
st.markdown(
    "Демонстрирует 5 контрольных проверок по ТЗ на загруженной ОСВ. "
    "Поддерживаемые форматы: **CSV, XLS, XLSX, HTML** (отчет 1С «Оборотно-сальдовая "
    "ведомость», сохраненный как Excel/HTML). MXL не поддерживается — сохраните отчет "
    "в 1С как Excel или HTML. Опционально загружается реестр документов для проверки "
    "расчетов с контрагентами."
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
    documents: pd.DataFrame, selected_periods: list[str]
) -> pd.DataFrame:
    """Оставляет операции не позднее конца последнего выбранного периода."""
    if documents is None or documents.empty:
        return documents
    ends = pd.to_datetime(
        pd.Series([p for p in selected_periods if p]), errors="coerce"
    ).dropna()
    if ends.empty:
        return documents
    return documents[documents["Дата"] <= ends.max()]


def run_audit(
    balances: pd.DataFrame,
    documents: Optional[pd.DataFrame],
    closing_accounts: list[str],
    ml_options: dict,
    checks: Optional[set[str]] = None,
    meta: Optional[dict] = None,
) -> AutoAuditor1C:
    """Собирает аудитора с заданными настройками и выполняет проверки."""
    auditor = AutoAuditor1C(
        balances,
        documents,
        closing_accounts=closing_accounts,
        checks=checks,
        meta=meta,
        balance_group_checks=chk_group_balances,
        ml_enabled=ml_options.get("ml_enabled", False),
        ml_amount_anomalies=ml_options.get("ml_amount_anomalies", True),
        ml_turnover_jumps=ml_options.get("ml_turnover_jumps", True),
        ml_duplicates=ml_options.get("ml_duplicates", True),
        dup_threshold=ml_options.get("dup_threshold", 90),
        anomaly_min_abs=ml_options.get("anomaly_min_abs", 1000.0),
    )
    auditor.run_audit()
    return auditor


def _render_results(result: dict) -> None:
    """Отображает результаты аудита с фильтрами (только отображение)."""
    auditor: AutoAuditor1C = result["auditor"]
    errors = result["errors"]
    report = result["report"]

    st.markdown("---")
    st.header("📋 Результаты проверки")

    reset = st.button("✖️ Сбросить результаты")
    if reset:
        del st.session_state["audit"]
        st.rerun()

    if report["status"] == "ok":
        st.success("🎉 Ошибок не найдено! Учет в порядке.")
        return

    st.error(f"🚨 Статус: {report['status_label']}")

    details = report["details"].copy()

    c1, c2, c3 = st.columns(3)
    c1.metric("Красных флагов", report["total_flags"])
    c2.metric("Общая сумма отклонений", f"{report['total_amount']:,.2f}")
    c3.metric("Проверок с находками", len(errors))

    available_checks = sorted(details["Проверка"].dropna().unique().tolist())
    available_accounts = sorted(details["Счет"].astype(str).dropna().unique().tolist())
    available_cps = sorted(details["Субконто"].astype(str).dropna().unique().tolist())

    with st.expander("🔎 Фильтры отображения (в Excel выгружается полный отчет)", expanded=False):
        f1, f2, f3 = st.columns(3)
        sel_checks = f1.multiselect("Проверки", options=available_checks, key="f_checks")
        sel_accounts = f2.multiselect("Счета", options=available_accounts, key="f_accounts")
        sel_cps = f3.multiselect("Контрагенты", options=available_cps, key="f_cps")

    details_view = details
    if sel_checks:
        details_view = details_view[details_view["Проверка"].isin(sel_checks)]
    if sel_accounts:
        details_view = details_view[details_view["Счет"].astype(str).isin(sel_accounts)]
    if sel_cps:
        details_view = details_view[details_view["Субконто"].astype(str).isin(sel_cps)]

    st.subheader("📈 Сводный отчет")
    if details_view.empty:
        st.info("По выбранным фильтрам строк нет.")
        summary_view = pd.DataFrame(columns=["Проверка", "Уровень", "Строк", "Сумма", "Рекомендации"])
    else:
        summary_view = details_view.groupby(["Проверка", "Уровень"], as_index=False).agg(
            Строк=("Сумма", "size"),
            Сумма=("Сумма", "sum"),
        )
        rec_map = report["summary"].set_index("Проверка")["Рекомендации"].to_dict()
        summary_view["Рекомендации"] = summary_view["Проверка"].map(rec_map)
    st.dataframe(summary_view, width="stretch", hide_index=True)

    st.subheader("🔍 Детальный отчет")
    if details_view.empty:
        st.info("Нет строк по выбранным фильтрам.")
    else:
        st.dataframe(details_view, width="stretch", hide_index=True)

    st.download_button(
        "💾 Выгрузить отчет в Excel",
        data=auditor.to_excel(),
        file_name="audit_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if pdf_available:
        try:
            st.download_button(
                "💾 Выгрузить отчет в PDF",
                data=auditor.to_pdf(),
                file_name="audit_report.pdf",
                mime="application/pdf",
            )
        except Exception as exc:
            st.caption(f"PDF недоступен: {exc}")
    else:
        st.caption("PDF-экспорт недоступен — установите fpdf2: `pip install fpdf2`")

    st.markdown("---")
    st.subheader("🧾 Детализация по проверкам")
    for res in errors:
        if sel_checks and res["title"] not in sel_checks:
            continue
        data = res["data"]
        if sel_accounts and "Счет" in data.columns:
            data = data[data["Счет"].astype(str).isin(sel_accounts)]
        if sel_cps and "Субконто" in data.columns:
            data = data[data["Субконто"].astype(str).isin(sel_cps)]
        if data.empty:
            continue
        icon = "🔴" if res["level"] == "error" else "🟡"
        with st.expander(f"{icon} {res['title']} — строк: {len(data)}, сумма: {res['amount']:,.2f}"):
            recommendation = RECOMMENDATIONS.get(res["title"], "")
            if recommendation:
                st.markdown(f"💡 **Рекомендация:** {recommendation}")
            st.dataframe(data, width="stretch", hide_index=True)


# ============ Боковая панель ============
st.sidebar.header("📥 Загрузка данных")
osv_file = st.sidebar.file_uploader(
    "ОСВ (CSV / XLS / XLSX / HTML)",
    type=["csv", "xls", "xlsx", "html", "htm"],
    key="osv"
)
doc_file = st.sidebar.file_uploader(
    "Движения/документы (CSV, опционально)",
    type=["csv"],
    key="docs"
)
use_mock = st.sidebar.button("Использовать тестовые данные")

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

try:
    if osv_file is not None:
        balances, source_info = load_osv_file(
            osv_file.name,
            osv_file.getvalue(),
            plan_override=plan_input
        )
    elif use_mock or "mock_data" in st.session_state:
        if use_mock:
            st.session_state["mock_data"] = {
                "balances": normalize_balances(pd.read_csv("sample_data.csv", dtype=str)),
                "documents": normalize_documents(pd.read_csv("sample_documents.csv", dtype=str)),
            }
        balances = st.session_state["mock_data"]["balances"]
        documents = st.session_state["mock_data"]["documents"]

    if doc_file is not None:
        documents = normalize_documents(load_csv(doc_file))
except ValueError as exc:
    st.sidebar.error(str(exc))
    st.stop()

if balances is None:
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

meta = {"organization": org_input.strip()}
real_periods = [p for p in selected_periods if p]
if real_periods:
    meta["period"] = ", ".join(real_periods)
if source_info.get("title"):
    meta["title"] = source_info["title"]

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
if st.button("🚀 Запустить Аудит", type="primary"):
    try:
        with st.spinner("Анализируем данные..."):
            auditor = run_audit(
                filtered,
                doc_filtered,
                closing_accounts,
                {
                    "ml_enabled": ml_enabled,
                    "ml_amount_anomalies": ml_amount_anomalies,
                    "ml_turnover_jumps": ml_turnover_jumps,
                    "ml_duplicates": ml_duplicates,
                    "dup_threshold": dup_threshold,
                    "anomaly_min_abs": anomaly_min_abs,
                },
                checks=checks,
                meta=meta,
            )
        st.session_state["audit"] = {
            "auditor": auditor,
            "errors": auditor.errors,
            "report": auditor.report(),
        }
    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")

if "audit" in st.session_state:
    _render_results(st.session_state["audit"])
