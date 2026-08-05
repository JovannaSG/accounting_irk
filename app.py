import io
import traceback

import pandas as pd
import streamlit as st

from auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    normalize_balances,
    normalize_documents,
)
from loaders import load_osv_file

st.set_page_config(page_title="ИИ-Аудитор 1С", page_icon="🕵️‍♂️", layout="wide")

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


# ---------------- Боковая панель ----------------
st.sidebar.header("📥 Загрузка данных")
osv_file = st.sidebar.file_uploader(
    "ОСВ (CSV / XLS / XLSX / HTML)", type=["csv", "xls", "xlsx", "html", "htm"], key="osv"
)
doc_file = st.sidebar.file_uploader("Движения/документы (CSV, опционально)", type=["csv"], key="docs")
use_mock = st.sidebar.button("Использовать тестовые данные")

with st.sidebar.expander("⚙️ Настройки проверок"):
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

# ---------------- Загрузка ----------------
balances = None
documents = None
source_info = {}

try:
    if osv_file is not None:
        balances, source_info = load_osv_file(osv_file.name, osv_file.getvalue(), plan_override=plan_input)
    elif use_mock:
        balances = normalize_balances(pd.read_csv("sample_data.csv", dtype=str))

    if doc_file is not None:
        documents = normalize_documents(load_csv(doc_file))
    elif use_mock:
        documents = normalize_documents(pd.read_csv("sample_documents.csv", dtype=str))
except ValueError as exc:
    st.sidebar.error(str(exc))
    st.stop()

if balances is None:
    st.info("👈 Загрузите файл ОСВ (CSV/XLS/XLSX/HTML) или нажмите «Использовать тестовые данные» в панели слева.")
    st.stop()

if source_info.get("title") or source_info.get("organization"):
    st.sidebar.caption(f"📄 Источник: {source_info.get('title') or ''}"
                       f"{(' | Орг.: ' + source_info['organization']) if source_info.get('organization') else ''}")

periods = sorted(b for b in balances["Период"].dropna().unique() if b != "")
periods.insert(0, "")  # вариант «без периода» для старых форматов
selected_periods = st.sidebar.multiselect("Период проверки", options=periods, default=periods, format_func=lambda p: p or "— без периода —")

closing_accounts = [a.strip() for a in closing_input.split(",") if a.strip()]

# ---------------- Исходные данные ----------------
st.subheader("📊 Исходные данные (Оборотно-сальдовая ведомость)")
mask = balances["Период"].isin(selected_periods)
filtered = balances[mask]
st.dataframe(filtered, width="stretch", hide_index=True)

if filtered.empty:
    st.warning("Выбранные периоды не содержат данных. Отмените фильтр по периодам.")
    st.stop()

if documents is not None:
    st.caption(f"📄 Загружен реестр документов: {len(documents)} операций")

# ---------------- Запуск проверки ----------------
if st.button("🚀 Запустить Аудит", type="primary"):
    try:
        with st.spinner("Анализируем данные..."):
            auditor = AutoAuditor1C(filtered, documents, closing_accounts=closing_accounts)
            errors = auditor.run_audit()
            report = auditor.report()

        st.markdown("---")
        st.header("📋 Результаты проверки")

        if report["status"] == "ok":
            st.success("🎉 Ошибок не найдено! Учет в порядке.")
        else:
            st.error(f"🚨 Статус: {report['status_label']}")

            c1, c2, c3 = st.columns(3)
            c1.metric("Красных флагов", report["total_flags"])
            c2.metric("Общая сумма отклонений", f"{report['total_amount']:,.2f}")
            c3.metric("Проверок с находками", len(errors))

            st.subheader("📈 Сводный отчет")
            summary = report["summary"].copy()
            summary["Сумма"] = summary["Сумма"].map(lambda v: f"{v:,.2f}")
            st.dataframe(summary, width="stretch", hide_index=True)

            st.subheader("🔍 Детальный отчет")
            details = report["details"].copy()
            details["Сумма"] = details["Сумма"].map(lambda v: f"{v:,.2f}")
            st.dataframe(details, width="stretch", hide_index=True)

            st.download_button(
                "💾 Выгрузить отчет в Excel",
                data=auditor.to_excel(),
                file_name="audit_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            st.markdown("---")
            st.subheader("🧾 Детализация по проверкам")
            for idx, res in enumerate(errors):
                icon = "🔴" if res["level"] == "error" else "🟡"
                with st.expander(f"{icon} {res['title']} — строк: {len(res['data'])}, сумма: {res['amount']:,.2f}"):
                    st.dataframe(res["data"], width="stretch", hide_index=True)

    except Exception as exc:
        st.error(f"Ошибка при выполнении проверки: {exc}")
        st.code(traceback.format_exc(), language="python")
