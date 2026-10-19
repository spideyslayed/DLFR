from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field

class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class OwnerSetupIn(RegisterIn):
    setup_key: str = Field(min_length=20, max_length=256)

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: str
    is_active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class RoleUpdateIn(BaseModel):
    role: Literal["admin", "farmer", "company", "worker", "supplier"]

class ActiveUpdateIn(BaseModel):
    is_active: bool

class PartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    kind: Literal["farmer", "company", "supplier", "both", "other"] = "farmer"
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=254)
    address: str = Field(default="", max_length=400)

class PartyOut(PartyIn):
    id: int
    model_config = ConfigDict(from_attributes=True)

class PartyLinkIn(BaseModel):
    party_id: int | None = None

class TransactionIn(BaseModel):
    date: date
    time_text: str = Field(default="", max_length=20)
    invoice: str = Field(default="", max_length=50)
    party_name: str = Field(min_length=1, max_length=180)
    party_kind: str = Field(default="farmer", max_length=30)
    crop: str = Field(min_length=1, max_length=80)
    variety: str = Field(default="", max_length=120)
    quantity_qtl: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    rate: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    extra_charges: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    deductions: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    paid: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    payment_method: str = Field(default="Cash", max_length=40)
    notes: str = Field(default="", max_length=4000)

class TransactionOut(BaseModel):
    id: int
    kind: str
    invoice: str
    date: date
    time_text: str
    party_name: str
    party_kind: str
    crop: str
    variety: str
    quantity_qtl: Decimal
    rate: Decimal
    extra_charges: Decimal
    deductions: Decimal
    total: Decimal
    paid: Decimal
    balance: Decimal
    payment_method: str
    notes: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ExpenseIn(BaseModel):
    date: date
    category: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    payee: str = Field(default="", max_length=180)
    transport_company: str = Field(default="", max_length=180)
    vehicle: str = Field(default="", max_length=50)
    notes: str = Field(default="", max_length=4000)

class ExpenseOut(ExpenseIn):
    id: int
    model_config = ConfigDict(from_attributes=True)

class PaymentIn(BaseModel):
    date: date
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    method: str = Field(default="Cash", max_length=40)
    reference: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)

class PaymentOut(PaymentIn):
    id: int
    transaction_id: int
    model_config = ConfigDict(from_attributes=True)

class WorkerIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(default="", max_length=40)
    job_title: str = Field(default="General worker", max_length=100)
    daily_wage: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    linked_user_id: int | None = None

class WorkerOut(WorkerIn):
    id: int
    is_active: bool
    model_config = ConfigDict(from_attributes=True)

class AttendanceIn(BaseModel):
    worker_id: int
    date: date
    status: Literal["present", "half", "absent"] = "present"
    overtime_hours: Decimal = Field(default=Decimal("0"), ge=0, le=24, max_digits=6, decimal_places=2)
    notes: str = Field(default="", max_length=1000)

class WagePaymentIn(BaseModel):
    worker_id: int
    date: date
    period_month: str = Field(pattern=r"^\d{4}-\d{2}$")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    method: str = Field(default="Cash", max_length=40)
    reference: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)

class WagePaymentOut(WagePaymentIn):
    id: int
    model_config = ConfigDict(from_attributes=True)
