import os
import json
import requests
from typing import Optional, Dict, Any, List

# API Base URL from environment or default to localhost:8000
API_BASE_URL = os.environ.get("AUDIT_API_URL", "http://127.0.0.1:8000")

class APIError(Exception):
    """Исключение для ошибок при обращении к бэкенду"""
    pass

class ConnectionAPIError(APIError):
    """Исключение при невозможности подключиться к бэкенду"""
    pass

def _handle_response(response: requests.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        raise APIError(f"Неизвестный ответ сервера (HTTP {response.status_code}): {response.text}")
        
    if response.status_code >= 400:
        detail = data.get("detail", "Неизвестная ошибка")
        raise APIError(str(detail))
    return data

def check_health() -> bool:
    try:
        r = requests.get(f"{API_BASE_URL}/health", timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False

def run_audit_mock(options: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_BASE_URL}/api/audit/mock"
    try:
        r = requests.post(url, json={"options": options}, timeout=60)
        return _handle_response(r)
    except requests.exceptions.ConnectionError:
        raise ConnectionAPIError("Не удалось подключиться к серверу API. Убедитесь, что бэкенд запущен.")
    except requests.RequestException as e:
        raise APIError(f"Ошибка запроса: {e}")

def run_audit_1c(
    api_url: str,
    api_user: str,
    api_pass: str,
    start_date: str,
    end_date: str,
    options: Dict[str, Any]
) -> Dict[str, Any]:
    url = f"{API_BASE_URL}/api/audit/1c"
    payload = {
        "url": api_url,
        "user": api_user,
        "password": api_pass,
        "start_date": start_date,
        "end_date": end_date,
        "options": options
    }
    try:
        r = requests.post(url, json=payload, timeout=120)
        return _handle_response(r)
    except requests.exceptions.ConnectionError:
        raise ConnectionAPIError("Не удалось подключиться к серверу API. Убедитесь, что бэкенд запущен.")
    except requests.RequestException as e:
        raise APIError(f"Ошибка запроса: {e}")

def run_audit_file(
    osv_name: str,
    osv_bytes: bytes,
    doc_name: Optional[str],
    doc_bytes: Optional[bytes],
    options: Dict[str, Any]
) -> Dict[str, Any]:
    url = f"{API_BASE_URL}/api/audit/file"
    
    files = {
        "osv_file": (osv_name, osv_bytes, "application/octet-stream")
    }
    if doc_name and doc_bytes:
        files["doc_file"] = (doc_name, doc_bytes, "application/octet-stream")
        
    # Options need to be mapped to individual form parameters
    data = {
        "checks": ",".join(options.get("checks", [])),
        "closing_accounts": ",".join(options.get("closing_accounts", [])),
        "plan_override": options.get("plan_override", ""),
        "organization": options.get("organization", ""),
        "periods": json.dumps(options.get("periods")) if options.get("periods") is not None else "",
        "balance_group_checks": options.get("balance_group_checks", False),
        "ml_enabled": options.get("ml_enabled", True),
        "ml_amount_anomalies": options.get("ml_amount_anomalies", True),
        "ml_turnover_jumps": options.get("ml_turnover_jumps", True),
        "ml_duplicates": options.get("ml_duplicates", True),
        "dup_threshold": options.get("dup_threshold", 90),
        "anomaly_min_abs": options.get("anomaly_min_abs", 1000.0),
    }
    
    try:
        r = requests.post(url, files=files, data=data, timeout=60)
        return _handle_response(r)
    except requests.exceptions.ConnectionError:
        raise ConnectionAPIError("Не удалось подключиться к серверу API. Убедитесь, что бэкенд запущен.")
    except requests.RequestException as e:
        raise APIError(f"Ошибка запроса: {e}")

def get_account_detail(audit_id: str, account_code: str) -> Dict[str, Any]:
    url = f"{API_BASE_URL}/api/account/detail"
    payload = {
        "audit_id": audit_id,
        "account_code": account_code
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        return _handle_response(r)
    except requests.exceptions.ConnectionError:
        raise ConnectionAPIError("Не удалось подключиться к серверу API. Убедитесь, что бэкенд запущен.")
    except requests.RequestException as e:
        raise APIError(f"Ошибка запроса: {e}")

def get_excel_report(audit_id: str) -> bytes:
    url = f"{API_BASE_URL}/api/audit/{audit_id}/excel"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code >= 400:
            _handle_response(r)
        return r.content
    except requests.RequestException as e:
        raise APIError(f"Ошибка выгрузки Excel: {e}")

def get_pdf_report(audit_id: str) -> bytes:
    url = f"{API_BASE_URL}/api/audit/{audit_id}/pdf"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code >= 400:
            _handle_response(r)
        return r.content
    except requests.RequestException as e:
        raise APIError(f"Ошибка выгрузки PDF: {e}")
