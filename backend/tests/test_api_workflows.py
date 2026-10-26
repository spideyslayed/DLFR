from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import User
from app.security import hash_password, create_access_token


@pytest.fixture
def dlfr_client():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestSession() as db:
        owner = User(name="DLFR Owner", email="dlfr-owner@example.com", password_hash=hash_password("OwnerTestPassword123!"), role="owner", is_active=True)
        db.add(owner)
        db.commit()
        db.refresh(owner)
        owner_headers = {"Authorization": f"Bearer {create_access_token(owner.id, owner.role)}"}
        owner_id = owner.id

    with TestClient(app) as client:
        yield client, owner_headers, owner_id

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def test_public_signup_is_farmer_and_cannot_manage_users(dlfr_client):
    client, owner_headers, _ = dlfr_client
    signup = client.post("/api/auth/register", json={
        "name": "Test Farmer", "email": "signup-farmer@example.com", "password": "FarmerPassword123!"
    })
    assert signup.status_code == 200
    assert signup.json()["user"]["role"] == "farmer"
    denied = client.get("/api/admin/users", headers={"Authorization": f"Bearer {signup.json()['access_token']}"})
    assert denied.status_code == 403
    users = client.get("/api/admin/users", headers=owner_headers)
    assert users.status_code == 200


def test_customer_portal_is_linked_and_payments_update_balance(dlfr_client):
    client, owner_headers, _ = dlfr_client
    party = client.post("/api/parties", headers=owner_headers, json={
        "name": "Farm Alpha", "kind": "farmer", "phone": "", "email": "", "address": ""
    })
    assert party.status_code == 201, party.text
    customer = client.post("/api/auth/register", json={
        "name": "Farmer Alpha Login", "email": "alpha-farmer@example.com", "password": "FarmerPassword123!"
    })
    assert customer.status_code == 200
    link = client.put(f"/api/admin/users/{customer.json()['user']['id']}/party-link", headers=owner_headers, json={"party_id": party.json()["id"]})
    assert link.status_code == 200, link.text
    bill = client.post("/api/transactions/purchase", headers=owner_headers, json={
        "date": "2026-10-18", "time_text": "10:30", "invoice": "DLFR-TEST-100", "party_name": "Farm Alpha",
        "party_kind": "farmer", "crop": "Wheat", "variety": "Lokwan", "quantity_qtl": "10.000",
        "rate": "2000.00", "extra_charges": "0", "deductions": "0", "paid": "500.00", "payment_method": "Cash", "notes": ""
    })
    assert bill.status_code == 201, bill.text
    bill_data = bill.json()
    assert Decimal(bill_data["balance"]) == Decimal("19500.00")
    payment = client.post(f"/api/transactions/{bill_data['id']}/payments", headers=owner_headers, json={
        "date": "2026-10-18", "amount": "1000.00", "method": "UPI", "reference": "TEST-REF", "notes": ""
    })
    assert payment.status_code == 201, payment.text
    portal = client.get("/api/portal/summary", headers={"Authorization": f"Bearer {customer.json()['access_token']}"})
    assert portal.status_code == 200, portal.text
    assert len(portal.json()["transactions"]) == 1
    assert Decimal(portal.json()["transactions"][0]["balance"]) == Decimal("18500.00")
    receipt = client.get(f"/api/portal/transactions/{bill_data['id']}/receipt", headers={"Authorization": f"Bearer {customer.json()['access_token']}"})
    assert receipt.status_code == 200
    assert receipt.headers["content-type"] == "application/pdf"


def test_worker_attendance_calculates_payroll(dlfr_client):
    client, owner_headers, _ = dlfr_client
    worker = client.post("/api/workers", headers=owner_headers, json={
        "name": "Test Loader", "phone": "", "job_title": "Loader", "daily_wage": "500.00", "linked_user_id": None
    })
    assert worker.status_code == 201, worker.text
    attendance = client.post("/api/attendance", headers=owner_headers, json={
        "worker_id": worker.json()["id"], "date": "2026-10-18", "status": "half", "overtime_hours": "0", "notes": ""
    })
    assert attendance.status_code == 200, attendance.text
    payroll = client.get("/api/payroll?month=2026-10", headers=owner_headers)
    assert payroll.status_code == 200, payroll.text
    assert Decimal(payroll.json()["total_earned"]) == Decimal("250.00")

def test_public_purchase_prices(dlfr_client):
    client, _, _ = dlfr_client
    # Public endpoint should be reachable without auth headers
    res = client.get("/api/public/purchase-prices")
    assert res.status_code == 200
    data = res.json()
    assert "prices" in data
    assert "date" in data
    