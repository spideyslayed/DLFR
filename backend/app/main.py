from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from pathlib import Path
from io import BytesIO
from email.message import EmailMessage
import hmac, json, logging, smtplib, sqlite3
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from .config import settings
from .database import Base, engine, get_db
from .models import User, Party, PartyLink, Transaction, Payment, Expense, Worker, Attendance, WagePayment, AuditLog
from .schemas import (RegisterIn, OwnerSetupIn, LoginIn, UserOut, RoleUpdateIn, ActiveUpdateIn,
                      PartyIn, PartyOut, PartyLinkIn, TransactionIn, TransactionOut,
                      ExpenseIn, ExpenseOut, PaymentIn, PaymentOut, WorkerIn, WorkerOut,
                      AttendanceIn, WagePaymentIn, WagePaymentOut)
from .security import hash_password, verify_password, create_access_token, get_current_user, require_owner, require_staff

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dlfr")
Base.metadata.create_all(bind=engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if settings.smtp_host and settings.smtp_from_email and settings.report_recipient_email:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.report_timezone))
            hour, minute = _parse_time(settings.daily_report_time)
            scheduler.add_job(send_scheduled_daily_report, "cron", hour=hour, minute=minute, id="dlfr-daily-report", replace_existing=True)
            mhour, mminute = _parse_time(settings.monthly_report_time)
            scheduler.add_job(send_scheduled_monthly_report, "cron", day=settings.monthly_report_day, hour=mhour, minute=mminute, id="dlfr-monthly-report", replace_existing=True)
            scheduler.start()
            log.info("DLFR scheduled email reports enabled")
        except Exception as exc:
            log.exception("Could not enable scheduled reports: %s", exc)
    else:
        log.info("Email scheduler disabled; configure SMTP and REPORT_RECIPIENT_EMAIL in .env")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)

app = FastAPI(title=settings.app_name, version="1.0.0", description="DLFR grain-trading and business operations", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",") if x.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
MONEY = Decimal("0.01")


def _parse_time(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":", 1)
        hour, minute = int(h), int(m)
        if hour not in range(24) or minute not in range(60): raise ValueError
        return hour, minute
    except Exception as exc:
        raise ValueError("Report time must use HH:MM, for example 20:00") from exc


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _audit(db: Session, owner_id: int, actor_id: int, action: str, entity: str = "", entity_id: int | None = None, details: str = ""):
    db.add(AuditLog(owner_id=owner_id, actor_id=actor_id, action=action, entity=entity, entity_id=entity_id, details=details[:4000]))


def _transaction_total(payload: TransactionIn) -> Decimal:
    total = (payload.quantity_qtl * payload.rate + payload.extra_charges - payload.deductions).quantize(MONEY, rounding=ROUND_HALF_UP)
    if total < 0:
        raise HTTPException(status_code=422, detail="Deductions cannot exceed quantity × rate plus extra charges")
    if payload.paid > total:
        raise HTTPException(status_code=422, detail="Paid amount cannot exceed total bill")
    return total


def _report_data(db: Session, owner_id: int, start: date, end: date) -> dict:
    tx = list(db.scalars(select(Transaction).where(Transaction.owner_id == owner_id, Transaction.date >= start, Transaction.date <= end).order_by(Transaction.date, Transaction.id)))
    ex = list(db.scalars(select(Expense).where(Expense.owner_id == owner_id, Expense.date >= start, Expense.date <= end).order_by(Expense.date, Expense.id)))
    purchases = [t for t in tx if t.kind == "purchase"]
    sales = [t for t in tx if t.kind == "sale"]
    purchase_total = _money(sum((t.total for t in purchases), Decimal("0")))
    sales_total = _money(sum((t.total for t in sales), Decimal("0")))
    expense_total = _money(sum((e.amount for e in ex), Decimal("0")))
    payment_rows = list(db.scalars(select(Payment).where(Payment.owner_id == owner_id, Payment.date >= start, Payment.date <= end)))
    payment_by_tx = {t.id: t.kind for t in db.scalars(select(Transaction).where(Transaction.owner_id == owner_id))}
    purchase_payments = _money(sum((p.amount for p in payment_rows if payment_by_tx.get(p.transaction_id) == "purchase"), Decimal("0")))
    sales_payments = _money(sum((p.amount for p in payment_rows if payment_by_tx.get(p.transaction_id) == "sale"), Decimal("0")))
    result = {
        "start": start.isoformat(), "end": end.isoformat(),
        "purchase_total": str(purchase_total),
        "sales_total": str(sales_total),
        "expense_total": str(expense_total),
        "simple_period_result": str(_money(sales_total - purchase_total - expense_total)),
        "purchase_quantity_qtl": str(sum((t.quantity_qtl for t in purchases), Decimal("0"))),
        "sale_quantity_qtl": str(sum((t.quantity_qtl for t in sales), Decimal("0"))),
        "purchase_payments_made_in_period": str(purchase_payments),
        "sales_payments_received_in_period": str(sales_payments),
        "outstanding_purchase_bills": str(_money(sum((t.balance for t in purchases), Decimal("0")))),
        "outstanding_sale_bills": str(_money(sum((t.balance for t in sales), Decimal("0")))),
        "transaction_count": len(tx), "expense_count": len(ex),
        "accounting_note": "Sales minus purchases minus expenses is only a cash/register summary, not certified profit. FIFO gross margin is available only where recorded purchases cover the sale quantities; opening stock, wastage, taxes and adjustments must be entered/reconciled separately."
    }
    # FIFO estimate using all transactions through report end; sales in the chosen period only affect reported COGS.
    all_tx = list(db.scalars(select(Transaction).where(Transaction.owner_id == owner_id, Transaction.date <= end).order_by(Transaction.date, Transaction.id)))
    inventory: dict[str, list[list[Decimal]]] = {}
    cogs_in_period = Decimal("0")
    fifo_complete = True
    for row in all_tx:
        crop_key = f"{row.crop.strip().casefold()}|{row.variety.strip().casefold()}"
        queue = inventory.setdefault(crop_key, [])
        qty = Decimal(row.quantity_qtl)
        if row.kind == "purchase":
            unit_cost = _money(row.rate + (row.extra_charges / qty if qty else 0) - (row.deductions / qty if qty else 0))
            queue.append([qty, unit_cost])
        else:
            remaining = qty
            cost = Decimal("0")
            while remaining > 0 and queue:
                take = min(remaining, queue[0][0])
                cost += take * queue[0][1]
                queue[0][0] -= take
                remaining -= take
                if queue[0][0] <= 0: queue.pop(0)
            if remaining > 0:
                fifo_complete = False
            if start <= row.date <= end:
                cogs_in_period += cost
    result["fifo_gross_margin_estimate"] = str(_money(sum((t.total for t in sales), Decimal("0")) - cogs_in_period)) if fifo_complete else None
    if not fifo_complete:
        result["fifo_note"] = "Not available: one or more recorded sales exceed the purchases available in this database. Enter opening stock/purchases first."
    else:
        result["fifo_note"] = "Estimate uses FIFO on recorded purchases only; verify opening stock, quality losses, freight allocation, taxes and inventory adjustments."
    return result


def _mail_report(subject: str, body: str) -> None:
    required = [settings.smtp_host, settings.smtp_from_email, settings.report_recipient_email]
    if not all(required):
        raise RuntimeError("Email is not configured. Set SMTP_HOST, SMTP_FROM_EMAIL and REPORT_RECIPIENT_EMAIL in backend/.env")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = settings.report_recipient_email
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


def send_scheduled_daily_report():
    from .database import SessionLocal
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.role == "owner"))
        if not owner: return
        day = datetime.now(ZoneInfo(settings.report_timezone)).date()
        data = _report_data(db, owner.id, day, day)
        _mail_report(f"DLFR Daily Report — {day.isoformat()}", json.dumps(data, indent=2))
    except Exception:
        log.exception("Daily report email failed")
    finally: db.close()


def send_scheduled_monthly_report():
    from .database import SessionLocal
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.role == "owner"))
        if not owner: return
        today_local = datetime.now(ZoneInfo(settings.report_timezone)).date()
        first_this_month = today_local.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
        data = _report_data(db, owner.id, start, end)
        _mail_report(f"DLFR Monthly Report — {start:%Y-%m}", json.dumps(data, indent=2))
    except Exception:
        log.exception("Monthly report email failed")
    finally: db.close()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "DLFR API", "version": "1.0.0"}

@app.post("/api/auth/register", response_model=dict)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(name=payload.name.strip(), email=email, password_hash=hash_password(payload.password), role="farmer")
    db.add(user); db.commit(); db.refresh(user)
    return {"access_token": create_access_token(user.id, user.role), "token_type": "bearer", "user": UserOut.model_validate(user).model_dump()}

@app.post("/api/auth/setup-owner", response_model=dict)
def setup_owner(payload: OwnerSetupIn, db: Session = Depends(get_db)):
    key = settings.owner_setup_key
    if not key: raise HTTPException(status_code=503, detail="Owner setup is disabled. Configure OWNER_SETUP_KEY in backend/.env first.")
    if not hmac.compare_digest(payload.setup_key, key): raise HTTPException(status_code=403, detail="Invalid owner setup key")
    if db.scalar(select(User.id).where(User.role == "owner")) is not None: raise HTTPException(status_code=409, detail="An owner account already exists")
    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)): raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(name=payload.name.strip(), email=email, password_hash=hash_password(payload.password), role="owner")
    db.add(user); db.commit(); db.refresh(user)
    _audit(db, user.id, user.id, "owner_created", "user", user.id, "Initial owner provisioned")
    db.commit()
    return {"access_token": create_access_token(user.id, user.role), "token_type": "bearer", "user": UserOut.model_validate(user).model_dump()}

@app.post("/api/auth/login", response_model=dict)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return {"access_token": create_access_token(user.id, user.role), "token_type": "bearer", "user": UserOut.model_validate(user).model_dump()}

@app.get("/api/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user

@app.get("/api/admin/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    return list(db.scalars(select(User).order_by(User.created_at.desc(), User.id.desc())))

@app.patch("/api/admin/users/{user_id}/role", response_model=UserOut)
def update_user_role(user_id: int, payload: RoleUpdateIn, db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    target = db.get(User, user_id)
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if target.id == owner.id or target.role == "owner": raise HTTPException(status_code=409, detail="The owner account cannot be changed here")
    if target.role == "admin" and payload.role != "admin":
        pass
    target.role = payload.role
    _audit(db, owner.id, owner.id, "role_changed", "user", target.id, f"Role set to {payload.role}")
    db.commit(); db.refresh(target)
    return target

@app.patch("/api/admin/users/{user_id}/status", response_model=UserOut)
def update_user_status(user_id: int, payload: ActiveUpdateIn, db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    target = db.get(User, user_id)
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if target.id == owner.id or target.role == "owner": raise HTTPException(status_code=409, detail="The owner account cannot be deactivated here")
    target.is_active = payload.is_active
    _audit(db, owner.id, owner.id, "account_status_changed", "user", target.id, f"is_active={payload.is_active}")
    db.commit(); db.refresh(target)
    return target

@app.get("/api/admin/party-links")
def list_party_links(db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    users = list(db.scalars(select(User).order_by(User.name)))
    links = {link.user_id: link for link in db.scalars(select(PartyLink).where(PartyLink.owner_id == owner.id))}
    parties = {p.id: p for p in db.scalars(select(Party).where(Party.owner_id == owner.id))}
    return [{"user_id": u.id, "user_name": u.name, "email": u.email, "role": u.role,
             "party_id": links[u.id].party_id if u.id in links else None,
             "party_name": parties[links[u.id].party_id].name if u.id in links and links[u.id].party_id in parties else None}
            for u in users if u.role in {"farmer", "company", "supplier"}]

@app.put("/api/admin/users/{user_id}/party-link")
def set_party_link(user_id: int, payload: PartyLinkIn, db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    target = db.get(User, user_id)
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if target.role not in {"farmer", "company", "supplier"}: raise HTTPException(status_code=422, detail="Only farmer, company or supplier accounts can link to a party")
    link = db.scalar(select(PartyLink).where(PartyLink.user_id == user_id))
    if payload.party_id is None:
        if link: db.delete(link)
        db.commit()
        return {"ok": True, "party_id": None}
    party = db.get(Party, payload.party_id)
    if not party or party.owner_id != owner.id: raise HTTPException(status_code=404, detail="Party not found in this workspace")
    existing = db.scalar(select(PartyLink).where(PartyLink.party_id == party.id, PartyLink.user_id != user_id))
    if existing: raise HTTPException(status_code=409, detail="This party is already linked to another portal login")
    if link: link.owner_id = owner.id; link.party_id = party.id
    else: db.add(PartyLink(owner_id=owner.id, user_id=user_id, party_id=party.id))
    _audit(db, owner.id, owner.id, "portal_party_linked", "user", user_id, f"party_id={party.id}")
    db.commit()
    return {"ok": True, "party_id": party.id, "party_name": party.name}

@app.get("/api/portal/summary")
def portal_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "worker":
        worker = db.scalar(select(Worker).where(Worker.linked_user_id == user.id, Worker.is_active.is_(True)))
        if not worker: return {"role": "worker", "linked": False, "message": "Your worker account has not been linked by DLFR yet.", "worker": None, "attendance": [], "wage_payments": []}
        attendance = list(db.scalars(select(Attendance).where(Attendance.owner_id == worker.owner_id, Attendance.worker_id == worker.id).order_by(Attendance.date.desc())))
        wages = list(db.scalars(select(WagePayment).where(WagePayment.owner_id == worker.owner_id, WagePayment.worker_id == worker.id).order_by(WagePayment.date.desc())))
        paid = _money(sum((w.amount for w in wages), Decimal("0")))
        attendance_units = sum((Decimal("1") if a.status == "present" else Decimal("0.5") if a.status == "half" else Decimal("0") for a in attendance), Decimal("0"))
        earned = _money(attendance_units * worker.daily_wage)
        return {"role":"worker", "linked":True, "worker":{"id":worker.id,"name":worker.name,"job_title":worker.job_title,"daily_wage":str(worker.daily_wage)}, "attendance":[{"date":a.date.isoformat(),"status":a.status,"overtime_hours":str(a.overtime_hours)} for a in attendance], "wage_payments":[{"date":w.date.isoformat(),"period_month":w.period_month,"amount":str(w.amount),"method":w.method} for w in wages], "total_wages_earned":str(earned), "total_wages_paid":str(paid), "wages_balance":str(_money(earned-paid)), "attendance_units":str(attendance_units)}
    if user.role not in {"farmer", "company", "supplier"}:
        raise HTTPException(status_code=403, detail="This account uses the business workspace, not the customer portal")
    link = db.scalar(select(PartyLink).where(PartyLink.user_id == user.id))
    if not link: return {"role": user.role, "linked": False, "message": "Your account has not been linked to a DLFR customer record yet. Contact DLFR to link it.", "party": None, "transactions": [], "payments": []}
    party = db.get(Party, link.party_id)
    if not party or party.owner_id != link.owner_id: raise HTTPException(status_code=404, detail="Linked customer record was not found")
    stmt = select(Transaction).where(Transaction.owner_id == link.owner_id, func.lower(Transaction.party_name) == party.name.lower())
    transactions = list(db.scalars(stmt.order_by(Transaction.date.desc(), Transaction.id.desc())))
    tx_ids = [t.id for t in transactions]
    payments = list(db.scalars(select(Payment).where(Payment.owner_id == link.owner_id, Payment.transaction_id.in_(tx_ids)).order_by(Payment.date.desc(), Payment.id.desc()))) if tx_ids else []
    purchases = [t for t in transactions if t.kind == "purchase"]
    sales = [t for t in transactions if t.kind == "sale"]
    total_billed = _money(sum((t.total for t in transactions), Decimal("0")))
    total_paid = _money(sum((t.paid for t in transactions), Decimal("0")))
    balance = _money(sum((t.balance for t in transactions), Decimal("0")))
    return {"role":user.role, "linked":True, "party":{"id":party.id,"name":party.name,"kind":party.kind,"phone":party.phone},
            "transactions":[TransactionOut.model_validate(t).model_dump(mode="json") for t in transactions],
            "payments":[PaymentOut.model_validate(p).model_dump(mode="json") for p in payments],
            "purchase_total":str(_money(sum((t.total for t in purchases), Decimal("0")))),
            "sale_total":str(_money(sum((t.total for t in sales), Decimal("0")))),
            "purchase_paid":str(_money(sum((t.paid for t in purchases), Decimal("0")))),
            "sale_paid":str(_money(sum((t.paid for t in sales), Decimal("0")))),
            "purchase_balance":str(_money(sum((t.balance for t in purchases), Decimal("0")))),
            "sale_balance":str(_money(sum((t.balance for t in sales), Decimal("0")))),
            "total_billed":str(total_billed), "total_paid":str(total_paid), "balance":str(balance)}

@app.get("/api/parties", response_model=list[PartyOut])
def list_parties(db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    return list(db.scalars(select(Party).where(Party.owner_id == owner.id).order_by(Party.name)))

@app.post("/api/parties", response_model=PartyOut, status_code=201)
def create_party(payload: PartyIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    existing = db.scalar(select(Party.id).where(Party.owner_id == owner.id, func.lower(Party.name) == payload.name.strip().lower()))
    if existing: raise HTTPException(status_code=409, detail="A party with this name already exists")
    party = Party(owner_id=owner.id, **payload.model_dump())
    db.add(party); db.flush(); _audit(db, owner.id, owner.id, "party_created", "party", party.id, party.name)
    db.commit(); db.refresh(party)
    return party

@app.get("/api/transactions", response_model=list[TransactionOut])
def list_transactions(kind: str = Query("all", pattern="^(all|purchase|sale)$"), start: date | None = None, end: date | None = None, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    if start and end and start > end: raise HTTPException(status_code=422, detail="Start date must be on or before end date")
    stmt = select(Transaction).where(Transaction.owner_id == owner.id)
    if kind != "all": stmt = stmt.where(Transaction.kind == kind)
    if start: stmt = stmt.where(Transaction.date >= start)
    if end: stmt = stmt.where(Transaction.date <= end)
    return list(db.scalars(stmt.order_by(Transaction.date.desc(), Transaction.id.desc())))

@app.post("/api/transactions/{kind}", response_model=TransactionOut, status_code=201)
def create_transaction(kind: str, payload: TransactionIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    if kind not in {"purchase", "sale"}: raise HTTPException(status_code=404, detail="Transaction type must be purchase or sale")
    total = _transaction_total(payload)
    invoice = payload.invoice.strip() or f"DLFR-{kind[:3].upper()}-{uuid4().hex[:10].upper()}"
    if db.scalar(select(Transaction.id).where(Transaction.owner_id == owner.id, Transaction.invoice == invoice)):
        raise HTTPException(status_code=409, detail="This invoice number already exists")
    fields = payload.model_dump(exclude={"invoice", "paid", "payment_method"})
    row = Transaction(owner_id=owner.id, kind=kind, invoice=invoice, **fields, total=total, paid=payload.paid, balance=_money(total-payload.paid), payment_method=payload.payment_method)
    db.add(row); db.flush()
    if payload.paid > 0:
        db.add(Payment(owner_id=owner.id, transaction_id=row.id, date=payload.date, amount=payload.paid, method=payload.payment_method, reference="Initial payment", notes="Initial payment recorded with bill"))
    _audit(db, owner.id, owner.id, "transaction_created", "transaction", row.id, f"{kind} invoice={invoice}")
    db.commit(); db.refresh(row)
    return row

@app.get("/api/transactions/{transaction_id}/payments", response_model=list[PaymentOut])
def list_payments(transaction_id: int, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    row = db.get(Transaction, transaction_id)
    if not row or row.owner_id != owner.id: raise HTTPException(status_code=404, detail="Transaction not found")
    return list(db.scalars(select(Payment).where(Payment.transaction_id == transaction_id, Payment.owner_id == owner.id).order_by(Payment.date.desc(), Payment.id.desc())))

@app.post("/api/transactions/{transaction_id}/payments", response_model=PaymentOut, status_code=201)
def add_payment(transaction_id: int, payload: PaymentIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    row = db.get(Transaction, transaction_id)
    if not row or row.owner_id != owner.id: raise HTTPException(status_code=404, detail="Transaction not found")
    if payload.amount > row.balance: raise HTTPException(status_code=422, detail="Payment exceeds the outstanding balance")
    payment = Payment(owner_id=owner.id, transaction_id=row.id, **payload.model_dump())
    row.paid = _money(row.paid + payload.amount)
    row.balance = _money(row.total - row.paid)
    _audit(db, owner.id, owner.id, "payment_recorded", "transaction", row.id, f"amount={payload.amount} method={payload.method}")
    db.add(payment); db.commit(); db.refresh(payment)
    return payment

def _receipt_pdf_bytes(row: Transaction) -> BytesIO:
    buffer = BytesIO(); pdf = canvas.Canvas(buffer, pagesize=A4); width, height = A4
    y = height - 54
    pdf.setTitle(f"DLFR Receipt {row.invoice}")
    pdf.setFont("Helvetica-Bold", 20); pdf.drawString(48, y, "DLFR")
    pdf.setFont("Helvetica", 9); pdf.drawString(48, y-18, "Dhan Laxmi Foods and Research Centre")
    pdf.setFont("Helvetica-Bold", 15); pdf.drawRightString(width-48, y, "PURCHASE RECEIPT" if row.kind == "purchase" else "SALES INVOICE")
    y -= 60; pdf.line(48, y, width-48, y); y -= 28
    entries = [("Invoice", row.invoice), ("Date / Time", f"{row.date}  {row.time_text}"), ("Party", row.party_name), ("Party type", row.party_kind), ("Crop / Variety", f"{row.crop} / {row.variety or '—'}"), ("Quantity", f"{row.quantity_qtl} quintals"), ("Rate per quintal", f"INR {row.rate:,.2f}"), ("Extra charges", f"INR {row.extra_charges:,.2f}"), ("Deductions", f"INR {row.deductions:,.2f}"), ("Total bill", f"INR {row.total:,.2f}"), ("Paid to date", f"INR {row.paid:,.2f}"), ("Outstanding balance", f"INR {row.balance:,.2f}"), ("Payment method", row.payment_method), ("Notes", row.notes or "—")]
    for label, value in entries:
        pdf.setFont("Helvetica-Bold", 10); pdf.drawString(48, y, label)
        pdf.setFont("Helvetica", 10); pdf.drawString(190, y, str(value)[:95].encode("latin-1", "replace").decode("latin-1"))
        y -= 22
        if y < 80: pdf.showPage(); y = height - 60
    pdf.setFont("Helvetica-Oblique", 8); pdf.drawString(48, 42, "Computer-generated record. Verify quantities and amounts before settlement.")
    pdf.save(); buffer.seek(0)
    return buffer

@app.get("/api/transactions/{transaction_id}/receipt")
def transaction_receipt(transaction_id: int, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    row = db.get(Transaction, transaction_id)
    if not row or row.owner_id != owner.id: raise HTTPException(status_code=404, detail="Transaction not found")
    buffer = _receipt_pdf_bytes(row)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{row.invoice}.pdf"'})

@app.get("/api/portal/transactions/{transaction_id}/receipt")
def portal_transaction_receipt(transaction_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in {"farmer", "company", "supplier"}: raise HTTPException(status_code=403, detail="Customer portal access required")
    link = db.scalar(select(PartyLink).where(PartyLink.user_id == user.id))
    if not link: raise HTTPException(status_code=404, detail="No customer record is linked to this login")
    party = db.get(Party, link.party_id)
    row = db.get(Transaction, transaction_id)
    if not party or not row or row.owner_id != link.owner_id or party.owner_id != link.owner_id or row.party_name.strip().casefold() != party.name.strip().casefold():
        raise HTTPException(status_code=404, detail="Transaction not found in your portal")
    buffer = _receipt_pdf_bytes(row)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{row.invoice}.pdf"'})

@app.get("/api/expenses", response_model=list[ExpenseOut])
def list_expenses(start: date | None = None, end: date | None = None, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    if start and end and start > end: raise HTTPException(status_code=422, detail="Start date must be on or before end date")
    stmt = select(Expense).where(Expense.owner_id == owner.id)
    if start: stmt = stmt.where(Expense.date >= start)
    if end: stmt = stmt.where(Expense.date <= end)
    return list(db.scalars(stmt.order_by(Expense.date.desc(), Expense.id.desc())))

@app.post("/api/expenses", response_model=ExpenseOut, status_code=201)
def create_expense(payload: ExpenseIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    row = Expense(owner_id=owner.id, **payload.model_dump())
    db.add(row); db.flush(); _audit(db, owner.id, owner.id, "expense_created", "expense", row.id, f"{row.category}: {row.amount}")
    db.commit(); db.refresh(row)
    return row

@app.get("/api/workers", response_model=list[WorkerOut])
def list_workers(include_inactive: bool = False, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    stmt = select(Worker).where(Worker.owner_id == owner.id)
    if not include_inactive: stmt = stmt.where(Worker.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Worker.name)))

@app.post("/api/workers", response_model=WorkerOut, status_code=201)
def create_worker(payload: WorkerIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    if payload.linked_user_id:
        linked = db.get(User, payload.linked_user_id)
        if not linked or linked.role != "worker": raise HTTPException(status_code=422, detail="Link only an existing Worker login account")
        if db.scalar(select(Worker.id).where(Worker.linked_user_id == linked.id)): raise HTTPException(status_code=409, detail="That login is already linked to a worker")
    worker = Worker(owner_id=owner.id, **payload.model_dump())
    db.add(worker); db.flush(); _audit(db, owner.id, owner.id, "worker_created", "worker", worker.id, worker.name)
    db.commit(); db.refresh(worker)
    return worker

@app.patch("/api/workers/{worker_id}/status", response_model=WorkerOut)
def update_worker_status(worker_id: int, payload: ActiveUpdateIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    worker = db.get(Worker, worker_id)
    if not worker or worker.owner_id != owner.id: raise HTTPException(status_code=404, detail="Worker not found")
    worker.is_active = payload.is_active
    db.commit(); db.refresh(worker); return worker

@app.get("/api/attendance")
def list_attendance(start: date | None = None, end: date | None = None, worker_id: int | None = None, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    stmt = select(Attendance).where(Attendance.owner_id == owner.id)
    if start: stmt = stmt.where(Attendance.date >= start)
    if end: stmt = stmt.where(Attendance.date <= end)
    if worker_id: stmt = stmt.where(Attendance.worker_id == worker_id)
    rows = list(db.scalars(stmt.order_by(Attendance.date.desc(), Attendance.worker_id)))
    return [{"id":r.id,"worker_id":r.worker_id,"date":r.date.isoformat(),"status":r.status,"overtime_hours":str(r.overtime_hours),"notes":r.notes} for r in rows]

@app.post("/api/attendance")
def mark_attendance(payload: AttendanceIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    worker = db.get(Worker, payload.worker_id)
    if not worker or worker.owner_id != owner.id: raise HTTPException(status_code=404, detail="Worker not found")
    row = db.scalar(select(Attendance).where(Attendance.worker_id == worker.id, Attendance.date == payload.date))
    if row:
        row.status = payload.status; row.overtime_hours = payload.overtime_hours; row.notes = payload.notes
    else:
        row = Attendance(owner_id=owner.id, **payload.model_dump()); db.add(row)
    _audit(db, owner.id, owner.id, "attendance_saved", "attendance", row.id, f"worker={worker.name} date={payload.date} status={payload.status}")
    db.commit(); db.refresh(row)
    return {"id":row.id,"worker_id":row.worker_id,"date":row.date.isoformat(),"status":row.status,"overtime_hours":str(row.overtime_hours),"notes":row.notes}

@app.get("/api/payroll")
def payroll(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    year, mon = map(int, month.split("-"))
    try: first = date(year, mon, 1)
    except ValueError: raise HTTPException(status_code=422, detail="Invalid month")
    next_first = date(year+1,1,1) if mon == 12 else date(year,mon+1,1)
    workers = list(db.scalars(select(Worker).where(Worker.owner_id == owner.id).order_by(Worker.name)))
    result = []
    for w in workers:
        attendance = list(db.scalars(select(Attendance).where(Attendance.owner_id == owner.id, Attendance.worker_id == w.id, Attendance.date >= first, Attendance.date < next_first)))
        units = sum(Decimal("1") if a.status == "present" else Decimal("0.5") if a.status == "half" else Decimal("0") for a in attendance)
        earned = _money(units * w.daily_wage)
        payments = list(db.scalars(select(WagePayment).where(WagePayment.owner_id == owner.id, WagePayment.worker_id == w.id, WagePayment.period_month == month)))
        paid = _money(sum((p.amount for p in payments), Decimal("0")))
        result.append({"worker_id":w.id,"name":w.name,"job_title":w.job_title,"daily_wage":str(w.daily_wage),"present_days":str(units),"attendance_days":len(attendance),"earned":str(earned),"paid":str(paid),"balance":str(_money(earned-paid)),"is_active":w.is_active})
    return {"month":month,"workers":result,"total_earned":str(_money(sum((Decimal(r["earned"]) for r in result),Decimal("0")))),"total_paid":str(_money(sum((Decimal(r["paid"]) for r in result),Decimal("0")))),"total_balance":str(_money(sum((Decimal(r["balance"]) for r in result),Decimal("0"))))}

@app.get("/api/wage-payments", response_model=list[WagePaymentOut])
def list_wage_payments(month: str | None = None, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    stmt = select(WagePayment).where(WagePayment.owner_id == owner.id)
    if month: stmt = stmt.where(WagePayment.period_month == month)
    return list(db.scalars(stmt.order_by(WagePayment.date.desc(), WagePayment.id.desc())))

@app.post("/api/wage-payments", response_model=WagePaymentOut, status_code=201)
def pay_wages(payload: WagePaymentIn, db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    worker = db.get(Worker, payload.worker_id)
    if not worker or worker.owner_id != owner.id: raise HTTPException(status_code=404, detail="Worker not found")
    row = WagePayment(owner_id=owner.id, **payload.model_dump())
    db.add(row); db.flush(); _audit(db, owner.id, owner.id, "wages_paid", "worker", worker.id, f"month={payload.period_month} amount={payload.amount}")
    db.commit(); db.refresh(row)
    return row

@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    data = _report_data(db, owner.id, date.min, date.max)
    today = date.today()
    today_data = _report_data(db, owner.id, today, today)
    return {"purchases": data["purchase_total"], "sales": data["sales_total"], "expenses": data["expense_total"], "simple_period_result": str(_money(Decimal(data["sales_total"])-Decimal(data["purchase_total"])-Decimal(data["expense_total"]))), "outstanding_purchases": data["outstanding_purchase_bills"], "outstanding_sales": data["outstanding_sale_bills"], "today": today_data, "fifo_gross_margin_estimate": data["fifo_gross_margin_estimate"], "fifo_note": data["fifo_note"], "note": data["accounting_note"]}

@app.get("/api/reports/summary")
def report_summary(start: date = Query(), end: date = Query(), db: Session = Depends(get_db), owner: User = Depends(require_staff)):
    if start > end: raise HTTPException(status_code=422, detail="Start date must be on or before end date")
    return _report_data(db, owner.id, start, end)

@app.post("/api/reports/email-now")
def email_report_now(period: str = Query("today", pattern="^(today|month)$"), db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    today = datetime.now(ZoneInfo(settings.report_timezone)).date()
    if period == "today": start = end = today
    else: start, end = today.replace(day=1), today
    data = _report_data(db, owner.id, start, end)
    try: _mail_report(f"DLFR {'Daily' if period == 'today' else 'Month-to-date'} Report — {start} to {end}", json.dumps(data, indent=2))
    except (RuntimeError, OSError, smtplib.SMTPException) as exc: raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok":True,"sent_to":settings.report_recipient_email,"start":str(start),"end":str(end)}

@app.get("/api/admin/export")
def export_workspace(db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    """Owner-only JSON backup of workspace data; contains sensitive business information."""
    def vals(model, where):
        return [dict(row.__dict__) for row in db.scalars(select(model).where(where))]
    # Explicit serializers avoid leaking SQLAlchemy internals and password hashes.
    users = [{"id":u.id,"name":u.name,"email":u.email,"role":u.role,"is_active":u.is_active,"created_at":u.created_at.isoformat() if u.created_at else None} for u in db.scalars(select(User))]
    parties = [{"id":p.id,"owner_id":p.owner_id,"name":p.name,"kind":p.kind,"phone":p.phone,"email":p.email,"address":p.address} for p in db.scalars(select(Party).where(Party.owner_id == owner.id))]
    transactions = [{"id":t.id,"kind":t.kind,"invoice":t.invoice,"date":str(t.date),"party_name":t.party_name,"party_kind":t.party_kind,"crop":t.crop,"variety":t.variety,"quantity_qtl":str(t.quantity_qtl),"rate":str(t.rate),"extra_charges":str(t.extra_charges),"deductions":str(t.deductions),"total":str(t.total),"paid":str(t.paid),"balance":str(t.balance),"payment_method":t.payment_method,"notes":t.notes} for t in db.scalars(select(Transaction).where(Transaction.owner_id == owner.id))]
    expenses = [{"id":e.id,"date":str(e.date),"category":e.category,"amount":str(e.amount),"payee":e.payee,"transport_company":e.transport_company,"vehicle":e.vehicle,"notes":e.notes} for e in db.scalars(select(Expense).where(Expense.owner_id == owner.id))]
    workers = [{"id":w.id,"name":w.name,"phone":w.phone,"job_title":w.job_title,"daily_wage":str(w.daily_wage),"is_active":w.is_active,"linked_user_id":w.linked_user_id} for w in db.scalars(select(Worker).where(Worker.owner_id == owner.id))]
    payments = [{"id":p.id,"transaction_id":p.transaction_id,"date":str(p.date),"amount":str(p.amount),"method":p.method,"reference":p.reference,"notes":p.notes} for p in db.scalars(select(Payment).where(Payment.owner_id == owner.id))]
    attendance = [{"id":a.id,"worker_id":a.worker_id,"date":str(a.date),"status":a.status,"overtime_hours":str(a.overtime_hours),"notes":a.notes} for a in db.scalars(select(Attendance).where(Attendance.owner_id == owner.id))]
    wage_payments = [{"id":w.id,"worker_id":w.worker_id,"date":str(w.date),"period_month":w.period_month,"amount":str(w.amount),"method":w.method,"reference":w.reference,"notes":w.notes} for w in db.scalars(select(WagePayment).where(WagePayment.owner_id == owner.id))]
    party_links = [{"user_id":l.user_id,"party_id":l.party_id,"created_at":l.created_at.isoformat() if l.created_at else None} for l in db.scalars(select(PartyLink).where(PartyLink.owner_id == owner.id))]
    return {"created_at":datetime.now().isoformat(),"workspace":"DLFR","users":users,"parties":parties,"transactions":transactions,"expenses":expenses,"workers":workers,"payments":payments,"attendance":attendance,"wage_payments":wage_payments,"party_links":party_links}

@app.get("/api/admin/audit-log")
def audit_log(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    records = list(db.scalars(select(AuditLog).where(AuditLog.owner_id == owner.id).order_by(AuditLog.created_at.desc()).limit(limit)))
    return [{"id":r.id,"actor_id":r.actor_id,"action":r.action,"entity":r.entity,"entity_id":r.entity_id,"details":r.details,"created_at":r.created_at.isoformat() if r.created_at else None} for r in records]

def _mandi_normalize(value: object) -> str:
    """Normalize API/UI labels for tolerant comparisons without inventing data."""
    return " ".join(str(value or "").casefold().strip().split())


def _mandi_date(value: object):
    """Parse AGMARKNET dates (normally DD/MM/YYYY) into date objects."""
    from datetime import datetime as _dt
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return _dt.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _mandi_commodity_matches(actual: object, selected: str) -> bool:
    actual_norm = _mandi_normalize(actual)
    selected_norm = _mandi_normalize(selected)
    aliases = {
        "paddy / rice": ("paddy", "rice", "dhan"),
        "gram / chana": ("gram", "chana"),
        "arhar / tur": ("arhar", "tur", "pigeon pea"),
        "mustard": ("mustard", "sarson"),
        "maize": ("maize", "corn"),
        "bajra": ("bajra", "pearl millet"),
        "soybean": ("soybean", "soyabean", "soya bean"),
        "urad": ("urad", "urd", "black gram"),
        "moong": ("moong", "mung", "green gram"),
        "masoor": ("masoor", "lentil"),
        "groundnut": ("groundnut", "peanut"),
        "sesame": ("sesame", "til"),
        "linseed": ("linseed", "flax"),
    }
    terms = aliases.get(selected_norm, (selected_norm,))
    return any(term in actual_norm for term in terms if term)


@app.get("/api/mandi/prices")
async def mandi_prices(
    state: str = Query("Rajasthan", min_length=2, max_length=100),
    district: str = Query("Kota", min_length=2, max_length=100),
    commodity: str = Query("Wheat", min_length=2, max_length=100),
    day: str = Query("today", pattern="^(yesterday|today|tomorrow)$"),
):
    local_today = datetime.now(ZoneInfo(settings.report_timezone)).date()
    requested = local_today + timedelta(days={"yesterday": -1, "today": 0, "tomorrow": 1}[day])
    base = {"available": False, "day": day, "date": requested.isoformat(),
            "state": state, "district": district, "commodity": commodity, "prices": []}

    if day == "tomorrow":
        return {**base, "message": "Official mandi prices are not published in advance. Tomorrow's price cannot be confirmed; use the latest published prices as reference, not as a guarantee."}
    if not settings.mandi_api_key:
        return {**base, "message": "Live price access is not configured. Set MANDI_API_KEY in the dlfr-api Render environment."}

    url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
    # The dataset exposes State as state.keyword. Arrival_Date is returned on
    # each record but is not listed as an exposed API filter, so date filtering
    # is done locally after retrieving the state's recent records.
    params = {
        "api-key": settings.mandi_api_key,
        "format": "json",
        "limit": 1000,
        "offset": 0,
        "filters[state.keyword]": state,
        "sort[arrival_date]": "desc",
    }

    try:
        # Retries transient connect errors and bypasses any accidental proxy
        # environment variables on the hosted service. Never log or return the
        # full request URL because the API key is sent as a query parameter.
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "DLFR-Mandi/1.0"},
        ) as client:
            response = await client.get(url, params=params)
            status_code = response.status_code
            if status_code in (401, 403):
                log.warning("Mandi provider rejected API key (HTTP %s)", status_code)
                raise HTTPException(status_code=502, detail="The government data service rejected MANDI_API_KEY. Update it in Render Environment and redeploy.")
            if status_code == 429:
                log.warning("Mandi provider rate limited the request (HTTP 429)")
                raise HTTPException(status_code=503, detail="The government data service is rate limiting requests. Please try again later.")
            response.raise_for_status()
            body = response.json()
    except HTTPException:
        raise
    except httpx.ConnectError as exc:
        log.warning("Mandi provider connection failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="DLFR could not establish a network connection to api.data.gov.in. The key was not validated by this request; retry later or check Render outbound connectivity.",
        ) from exc
    except httpx.TimeoutException as exc:
        log.warning("Mandi provider request timed out (%s)", type(exc).__name__)
        raise HTTPException(status_code=504, detail="The government mandi service timed out. Please try again later.") from exc
    except httpx.HTTPStatusError as exc:
        log.warning("Mandi provider returned HTTP %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail=f"The government mandi service returned HTTP {exc.response.status_code}.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Could not read mandi response (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="DLFR could not read a valid response from the government mandi service.") from exc

    if not isinstance(body, dict) or body.get("status") not in ("ok", None):
        provider_message = str(body.get("message", "unknown provider response")) if isinstance(body, dict) else "invalid JSON shape"
        log.warning("Mandi provider response status not ok: %s", provider_message[:120])
        raise HTTPException(status_code=502, detail="The government mandi service did not return a successful dataset response.")

    records = body.get("records", [])
    requested_state = _mandi_normalize(state)
    requested_district = _mandi_normalize(district)
    prices = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if _mandi_normalize(record.get("state")) != requested_state:
            continue
        if _mandi_normalize(record.get("district")) != requested_district:
            continue
        if not _mandi_commodity_matches(record.get("commodity"), commodity):
            continue
        if _mandi_date(record.get("arrival_date")) != requested:
            continue
        prices.append({
            "state": record.get("state", ""),
            "district": str(record.get("district", "")).strip(),
            "market": record.get("market", ""),
            "commodity": record.get("commodity", ""),
            "variety": record.get("variety", ""),
            "grade": record.get("grade", ""),
            "min_price": record.get("min_price"),
            "max_price": record.get("max_price"),
            "modal_price": record.get("modal_price"),
            "arrival_date": record.get("arrival_date", ""),
        })

    # Report only live records that match the user's date/location/crop.
    # An empty array remains an honest no-records result, never fabricated prices.
    return {
        **base,
        "available": bool(prices),
        "prices": prices,
        "source": "Government of India Open Government Data Platform / AGMARKNET",
        "message": (
            "Official records returned by the configured feed; prices vary by market, variety and grade."
            if prices else
            "No published record matched this location, crop and date in the fetched records. Data publication can lag; try yesterday or another nearby district."
        ),
    }
