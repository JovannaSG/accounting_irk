import io
import os
import json
import uuid
import time
import logging
from typing import Any
from datetime import datetime

import pandas as pd
from fastapi import (
    FastAPI, HTTPException,
    UploadFile, File, Form,
    status, BackgroundTasks
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

from core.api_client import OneCClient
from core.auditor import (
    AutoAuditor1C,
    DEFAULT_CLOSING_ACCOUNTS,
    normalize_balances,
    normalize_documents,
)
from core.loaders import load_osv_file

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("audit-server")

app = FastAPI(
    title="ИИ-Аудитор 1С API",
    description="Бэкенд сервис для ИИ-Аудитора 1С"
)

# Allow CORS for Streamlit
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store: {audit_id: {"balances_df": df, "documents_df": df, "meta": dict, "source": dict, "created_at": float}}
cache_store: dict[str, dict[str, Any]] = {}

def clean_old_cache():
    now = time.time()
    to_delete = []
    for k, v in cache_store.items():
        if now - v.get("created_at", 0) > 3600:  # 1 hour TTL
            to_delete.append(k)
    for k in to_delete:
        cache_store.pop(k, None)
    if to_delete:
        logger.info(f"Очищен устаревший кэш сессий: {to_delete}")

# API Models
class AuditOptions(BaseModel):
    checks: list[str] = Field(default=["red_balance", "expanded_balance", "unclosed_month_end", "account_000", "settlements"])
    closing_accounts: list[str] = Field(default=DEFAULT_CLOSING_ACCOUNTS)
    plan_override: str | None = ""
    organization: str | None = ""
    periods: list[str] | None = None
    balance_group_checks: bool = False
    ml_enabled: bool = True
    ml_amount_anomalies: bool = True
    ml_turnover_jumps: bool = True
    ml_duplicates: bool = True
    dup_threshold: int = 90
    anomaly_min_abs: float = 1000.0

class Audit1CRequest(BaseModel):
    url: str
    user: str
    password: str
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    options: AuditOptions

class AuditMockRequest(BaseModel):
    options: AuditOptions

class AccountDetailRequest(BaseModel):
    audit_id: str
    account_code: str

# Helper functions
def build_summary_view_api(details_df: pd.DataFrame, db_name: str) -> pd.DataFrame:
    if details_df is None or details_df.empty:
        return pd.DataFrame(columns=["Имя Базы", "Счет", "Вид нарушений", "Период(ы)", "Дата просмотра"])
    
    d = details_df.copy()
    d["Счет"] = d["Счет"].astype(str).fillna("")
    d["Период"] = d["Период"].astype(str).fillna("")
    d["Проверка"] = d["Проверка"].astype(str).fillna("")
    
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    by_account = {}
    
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

def run_audit_core(
    balances: pd.DataFrame,
    documents: pd.DataFrame | None,
    options: AuditOptions,
    db_name: str,
    source_info: dict,
) -> dict[str, Any]:
    # Filter balances by selected periods if specified
    filtered_balances = balances.copy()
    if options.periods is not None:
        filtered_balances = filtered_balances[filtered_balances["Период"].isin(options.periods)]
    
    if filtered_balances.empty:
        raise ValueError("Выбранные периоды не содержат данных.")

    # Filter documents by period
    doc_filtered = None
    if documents is not None:
        doc_filtered = documents.copy()
        if options.periods:
            ends = pd.to_datetime(pd.Series([p for p in options.periods if p]), errors="coerce").dropna()
            if not ends.empty:
                doc_filtered = doc_filtered[doc_filtered["Дата"] <= ends.max()]
    
    # Run auditor
    meta = {"organization": options.organization or ""}
    real_periods = [p for p in (options.periods or []) if p]
    if real_periods:
        meta["period"] = ", ".join(real_periods)
    if source_info.get("title"):
        meta["title"] = source_info["title"]

    auditor = AutoAuditor1C(
        filtered_balances,
        doc_filtered,
        closing_accounts=options.closing_accounts,
        checks=set(options.checks),
        meta=meta,
        balance_group_checks=options.balance_group_checks,
        ml_enabled=options.ml_enabled,
        ml_amount_anomalies=options.ml_amount_anomalies,
        ml_turnover_jumps=options.ml_turnover_jumps,
        ml_duplicates=options.ml_duplicates,
        dup_threshold=options.dup_threshold,
        anomaly_min_abs=options.anomaly_min_abs,
    )
    auditor.run_audit()
    report = auditor.report()
    
    # Redesigned summary
    summary_df = build_summary_view_api(report["details"], db_name)
    
    # Generate audit id
    audit_id = str(uuid.uuid4())
    cache_store[audit_id] = {
        "balances_df": filtered_balances,
        "documents_df": doc_filtered,
        "meta": meta,
        "source": source_info,
        "created_at": time.time(),
        "auditor": auditor,
    }
    
    # Map errors
    errors_list = []
    for err in auditor.errors:
        errors_list.append({
            "title": err["title"],
            "level": err["level"],
            "amount": err["amount"],
            "data": err["data"].to_dict(orient="records"),
        })

    return {
        "audit_id": audit_id,
        "db_name": db_name,
        "status": report["status"],
        "status_label": report["status_label"],
        "total_flags": report["total_flags"],
        "balances": filtered_balances.to_dict(orient="records"),
        "summary": summary_df.to_dict(orient="records"),
        "details": report["details"].to_dict(orient="records"),
        "errors": errors_list,
    }

# Endpoints
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/audit/mock")
def audit_mock(req: AuditMockRequest, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(clean_old_cache)
    try:
        balances = normalize_balances(pd.read_csv(os.path.join(_DATA_DIR, "sample_data.csv"), dtype=str))
        documents = normalize_documents(pd.read_csv(os.path.join(_DATA_DIR, "sample_documents.csv"), dtype=str))
        
        source_info = {
            "title": "ОСВ (Тестовая)",
            "period": "Январь — Июнь 2026",
            "organization": "Демонстрационная база",
        }
        return run_audit_core(balances, documents, req.options, "Тестовая база", source_info)
    except Exception as exc:
        logger.exception("Error running mock audit")
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/audit/1c")
def audit_1c(req: Audit1CRequest, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(clean_old_cache)
    try:
        client = OneCClient(req.url.strip(), req.user.strip(), req.password)
        start_s = req.start_date + "T00:00:00"
        end_s = req.end_date + "T23:59:59"
        
        balances = client.fetch_osv_monthly(start_s, end_s)
        
        source_info = {
            "title": "ОСВ (1С:Фреш)",
            "period": f"{req.start_date} — {req.end_date}",
            "organization": "",
            # Store credentials for potential subconto fetches
            "url": req.url,
            "user": req.user,
            "password": req.password,
            "start_s": start_s,
            "end_s": end_s,
        }
        return run_audit_core(balances, None, req.options, req.url.strip(), source_info)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Error running 1C audit")
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/audit/file")
async def audit_file(
    bg_tasks: BackgroundTasks,
    osv_file: UploadFile = File(...),
    doc_file: UploadFile | None = File(None),
    checks: str = Form("red_balance,expanded_balance,unclosed_month_end,account_000,settlements"),
    closing_accounts: str = Form(""),
    plan_override: str = Form(""),
    organization: str = Form(""),
    periods: str | None = Form(None),
    balance_group_checks: bool = Form(False),
    ml_enabled: bool = Form(True),
    ml_amount_anomalies: bool = Form(True),
    ml_turnover_jumps: bool = Form(True),
    ml_duplicates: bool = Form(True),
    dup_threshold: int = Form(90),
    anomaly_min_abs: float = Form(1000.0),
):
    bg_tasks.add_task(clean_old_cache)
    try:
        osv_content = await osv_file.read()
        
        balances, source_info = load_osv_file(
            osv_file.filename,
            osv_content,
            plan_override=plan_override
        )
        
        documents = None
        if doc_file is not None:
            doc_content = await doc_file.read()
            # Simple pandas read from uploaded CSV/Excel
            if doc_file.filename.endswith(".csv"):
                # Try UTF-8 then CP1251
                try:
                    raw_docs = pd.read_csv(io.BytesIO(doc_content), dtype=str, encoding="utf-8-sig")
                except UnicodeDecodeError:
                    raw_docs = pd.read_csv(io.BytesIO(doc_content), dtype=str, encoding="cp1251")
            else:
                engine = "xlrd" if doc_file.filename.endswith(".xls") else "openpyxl"
                raw_docs = pd.read_excel(io.BytesIO(doc_content), dtype=str, engine=engine)
            documents = normalize_documents(raw_docs)
            
        # Parse closing accounts
        closing_list = [a.strip() for a in closing_accounts.split(",") if a.strip()] if closing_accounts else DEFAULT_CLOSING_ACCOUNTS
        
        # Parse periods
        periods_list = None
        if periods:
            try:
                periods_list = json.loads(periods)
            except (ValueError, TypeError):
                periods_list = [p.strip() for p in periods.split(",") if p.strip()]
        
        # Parse checks list
        checks_list = [c.strip() for c in checks.split(",") if c.strip()]
        
        options = AuditOptions(
            checks=checks_list,
            closing_accounts=closing_list,
            plan_override=plan_override,
            organization=organization,
            periods=periods_list,
            balance_group_checks=balance_group_checks,
            ml_enabled=ml_enabled,
            ml_amount_anomalies=ml_amount_anomalies,
            ml_turnover_jumps=ml_turnover_jumps,
            ml_duplicates=ml_duplicates,
            dup_threshold=dup_threshold,
            anomaly_min_abs=anomaly_min_abs,
        )
        
        return run_audit_core(balances, documents, options, osv_file.filename, source_info)
        
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Error running file audit")
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/account/detail")
def account_detail(req: AccountDetailRequest):
    audit_id = req.audit_id
    account_code = req.account_code
    
    if audit_id not in cache_store:
        raise HTTPException(status_code=404, detail="Сессия аудита не найдена или устарела. Пожалуйста, запустите аудит заново.")
        
    session = cache_store[audit_id]
    balances_df = session["balances_df"]
    
    # 1) OSV by Period (by months)
    # Filter the already loaded balances for this account and sort by period
    # Note: Period is string
    df_acc = balances_df[balances_df["Счет"] == account_code]
    by_period = df_acc.sort_values("Период").to_dict(orient="records")
    
    # 2) OSV by Subconto (drill-down with analytics)
    by_subconto = []
    source_info = session["source"]
    
    if source_info.get("url"): # 1C OData source
        try:
            client = OneCClient(source_info["url"].strip(), source_info["user"].strip(), source_info["password"])
            sub_df = client.fetch_osv_account_subconto(source_info["start_s"], source_info["end_s"], account_code)
            by_subconto = sub_df.to_dict(orient="records")
        except Exception as exc:
            logger.exception("Failed to fetch subconto from 1C")
            # We will return empty list or fallback to client side
            by_subconto = []
    else: # File or Mock source - subconto rows might be directly in the loaded balances_df
        # If balances_df contains rows with non-empty Subconto and matching Счет, group them
        df_sub = balances_df[(balances_df["Счет"] == account_code) & (balances_df["Субконто"] != "-")]
        if not df_sub.empty:
            by_subconto = df_sub.to_dict(orient="records")
            
    return {
        "by_period": by_period,
        "by_subconto": by_subconto,
    }

@app.get("/api/audit/{audit_id}/excel")
def export_excel(audit_id: str):
    if audit_id not in cache_store:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    auditor = cache_store[audit_id]["auditor"]
    excel_bytes = auditor.to_excel()
    
    from fastapi.responses import Response
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=audit_report.xlsx"}
    )

@app.get("/api/audit/{audit_id}/pdf")
def export_pdf(audit_id: str):
    if audit_id not in cache_store:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    auditor = cache_store[audit_id]["auditor"]
    try:
        pdf_bytes = auditor.to_pdf()
        from fastapi.responses import Response
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=audit_report.pdf"}
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
