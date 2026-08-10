import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_mock_audit_returns_audit_payload(client):
    r = client.post("/api/audit/mock", json={"options": {}})
    assert r.status_code == 200
    data = r.json()
    assert data["audit_id"]
    assert data["db_name"]
    assert data["status"] in ("ok", "warning", "error")
    assert isinstance(data["balances"], list)
    assert isinstance(data["summary"], list)
    assert isinstance(data["details"], list)
    assert isinstance(data["errors"], list)
    assert data["total_flags"] >= 0
    # Summary has redesigned columns with unified account row
    assert data["summary"][0]["Счет"]
    assert "Вид нарушений" in data["summary"][0]


def test_account_detail_for_mock(client):
    r = client.post("/api/audit/mock", json={"options": {}})
    audit = r.json()
    account = audit["summary"][0]["Счет"]

    r2 = client.post("/api/account/detail", json={
        "audit_id": audit["audit_id"],
        "account_code": account,
    })
    assert r2.status_code == 200
    detail = r2.json()
    assert "by_period" in detail
    assert "by_subconto" in detail
    # For mock source, by_period returns rows of the loaded balances for the account
    if detail["by_period"]:
        assert detail["by_period"][0]["Счет"] == account


def test_account_detail_unknown_session(client):
    r = client.post("/api/account/detail", json={
        "audit_id": "no-such-id",
        "account_code": "51",
    })
    assert r.status_code == 404


def test_1c_audit_error_is_mapped_to_400(monkeypatch, client):
    def boom(self, start, end):
        raise ValueError("OData вернул 401 Unauthorized")

    monkeypatch.setattr(server.OneCClient, "fetch_osv_monthly", boom)
    r = client.post("/api/audit/1c", json={
        "url": "https://example.com/base",
        "user": "u",
        "password": "p",
        "start_date": "2026-01-01",
        "end_date": "2026-02-28",
        "options": {},
    })
    assert r.status_code == 400
    assert "401" in r.json()["detail"]


def test_excel_and_pdf_export(client):
    r = client.post("/api/audit/mock", json={"options": {}})
    audit_id = r.json()["audit_id"]

    r_excel = client.get(f"/api/audit/{audit_id}/excel")
    assert r_excel.status_code == 200
    assert r_excel.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
    assert len(r_excel.content) > 0

    r_pdf = client.get(f"/api/audit/{audit_id}/pdf")
    assert r_pdf.status_code == 200
    assert r_pdf.headers["content-type"] == "application/pdf"
    assert len(r_pdf.content) > 0
