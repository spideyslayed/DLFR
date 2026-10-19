# DLFR — Dhan Laxmi Foods and Research Centre

A fuller local full-stack app for grain trading built with Python / FastAPI / SQLAlchemy and React / JavaScript / Vite. It extends the existing DLFR starter project and keeps existing SQLite tables intact. New tables are added automatically on startup.

## Implemented application workflows

- Login and public registration (public sign-ups are always `farmer`; they cannot become owner/admin).
- One-time protected owner setup endpoint using `OWNER_SETUP_KEY`.
- Owner-only people and access settings: role changes, account activation/deactivation, portal-party linking, audit log.
- Farmer/company/supplier portal showing the records tied to the customer party record selected by the owner, plus their bill/payment totals.
- Worker portal showing assigned attendance and recorded wage payments if a worker login is linked to a worker profile.
- Purchase and sales registers: date/time, invoice, party, crop, variety, quantity in quintals, per-quintal rate, charges, deductions, paid and balance.
- Payment ledger with validation that a payment cannot exceed the transaction balance.
- Print-ready PDF receipt / invoice per transaction, plus native file sharing where supported and WhatsApp text fallback.
- Customer directory.
- Expenses including transport company, driver/payee and vehicle.
- Worker profiles, daily attendance (present/half/absent), payroll by month, wage-payment ledger.
- Date-range reports, CSV/JSON export, print action, total receivables/payables, FIFO margin estimate, and period-based payments/outstanding data.
- FIFO-based gross-margin estimate when the recorded purchases cover recorded sales quantities; warns when opening stock / purchases are insufficient.
- Mandi price endpoint for published government records via data.gov.in API (requires API key). It refuses to invent tomorrow's rates.
- SMTP daily and monthly report scheduling when SMTP environment variables are configured; owner can also send a report manually from Settings.
- Owner-only JSON workspace export endpoint and audit events.
- Responsive DLFR UI, API docs and calculation tests.

## Important accounting/security notes

This is a complete **local-use MVP**, not a certified accounting system or a promise of production readiness. It should not be exposed publicly until a developer performs a security review, configures HTTPS and strong secrets, sets up database backups and restore drills, rate limiting, privacy policy, logging/monitoring, and tests on the real hosting environment. No accounting program can guarantee exact profit unless opening stock, wastage, returns, taxes, freight allocation and adjustments are entered consistently. FIFO margin is explicitly labeled an estimate.

Customer portal visibility is based on an owner-approved `PartyLink` and matching the transaction `party_name` to that party's name (case-insensitive). Use the exact name shown in the customer directory when recording a bill. Before public launch, migrate transactions to a true `party_id` foreign key and review portal authorization again.

Mandi data is unavailable until `MANDI_API_KEY` is configured. Official future-day prices are not published in advance, so the API does not invent a tomorrow price. SMTP reports remain disabled until configured with a supported mail provider.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- Internet connection during dependency installation

## Setup on Windows

The easiest way is to double-click the included launcher scripts:

- `backend\START_BACKEND.bat` creates a virtual environment, installs dependencies the first time, and starts the Python API.
- `frontend\START_WEBSITE.bat` installs website dependencies on first run and starts Vite.
- `backend\BACKUP_DATABASE.bat` makes a timestamped copy of the SQLite database. Stop the backend first for the safest backup.

Open one launcher for the backend and another for the website. Keep both terminal windows open. Open the Vite Local URL printed in the frontend window (usually `http://localhost:5173`). API docs are at `http://127.0.0.1:8000/docs`.

You can also use PowerShell manually. Open two PowerShell windows. The folder structure should look like `DLFR_Pro\backend` and `DLFR_Pro\frontend`.

### 1. Backend

```powershell
Set-Location "C:\path\to\DLFR_Pro\backend"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `backend\.env`. Change `JWT_SECRET` to a long random secret and set `OWNER_SETUP_KEY` to a random secret (at least 20 characters). Start API:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

API docs: http://127.0.0.1:8000/docs

### 2. Create owner account once

Public signup always creates a Farmer. Use the API docs at `http://127.0.0.1:8000/docs` to call `POST /api/auth/setup-owner` once, with `name`, `email`, `password`, and the exact `setup_key` value from `.env`. The endpoint refuses to create a second owner. Then sign into the website as that owner. Keep the setup key private; after owner provisioning, you may clear `OWNER_SETUP_KEY` and restart the backend.

### 3. Frontend

In a second PowerShell window:

```powershell
Set-Location "C:\path\to\DLFR_Pro\frontend"
npm install
npm run dev
```

Open the Vite address printed in the terminal, usually http://localhost:5173.

## 10-minute public demo deployment (Render)

This `render.yaml` creates a public API, a static website, and a **temporary free PostgreSQL database for demo/testing only**. Render's free PostgreSQL databases expire after 30 days and have no backups; do not enter real farmer/customer financial records until moving to a durable paid database, checking backups/restore, and reviewing security. Free API instances may sleep when idle. See Render's current limitations before proceeding: https://render.com/docs/free.

1. Push the whole `DLFR_Pro` folder to a private GitHub repository. Confirm `.env`, `dlfr.db`, `.venv`, `node_modules`, and `dist` are not committed (the included `.gitignore` excludes local secrets/data).
2. In Render, choose **New → Blueprint**, connect the repository, and select its branch. Render will read `render.yaml` and deploy the API, website and temporary demo database.
3. When deploy completes, open the API service's **Environment** page and set `CORS_ORIGINS` to the exact public website URL (include `https://`, with no trailing slash), then save/redeploy. The front end gets the API URL from `RENDER_EXTERNAL_URL`.
4. Open `https://<your-api-service>.onrender.com/docs`; test `GET /api/health`. In `POST /api/auth/setup-owner`, create a **new cloud owner** using the owner setup key generated by Render for that API service. Your local database does not automatically move to the cloud.
5. Open the public website URL and sign in. Test farmer registration, owner-only user settings, party linking and customer record isolation using dummy data only.

**Do not call this a production business launch yet.** The blueprint is for a public demo. Before real-world use, switch to a non-expiring database with backups, migrate data deliberately, review auth/role/data isolation, change credentials that have appeared in screenshots, configure CORS and email/mandi integrations, and complete a security review.

## Optional service configuration

### Mandi prices

Obtain your own API key from the Open Government Data Platform at data.gov.in and set `MANDI_API_KEY` in `backend\.env`. Restart the backend. The UI uses state, district, commodity and date scope. If the provider has not published records for that date, the app says no records were returned.

### Scheduled email reports

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `REPORT_RECIPIENT_EMAIL`, `DAILY_REPORT_TIME`, `MONTHLY_REPORT_DAY`, and `MONTHLY_REPORT_TIME` in `backend\.env`. Daily and monthly jobs only run while the backend process is running. Use an app password/API SMTP credential if required by your email provider. Do not commit `.env` or share secrets.

## Existing installation / existing records

1. Stop both servers using `Ctrl+C` in their terminals.
2. Back up your current `backend\dlfr.db` (use `BACKUP_DATABASE.bat` or copy the file while the server is stopped).
3. Extract this ZIP, then copy its `backend` and `frontend` folders into your existing `DLFR_Pro` folder and choose **Replace files** when asked.
4. **Keep your existing `backend\dlfr.db` and `backend\.env`. Do not replace or delete them.** The ZIP intentionally excludes the local database and secrets.
5. Run `python -m pip install -r requirements.txt` from `backend`, and `npm install` from `frontend` if dependencies changed, then start both servers again.

The existing `users`, `parties`, `transactions`, and `expenses` table definitions are preserved; new workflow tables are created automatically by SQLAlchemy. Always keep a separate database backup before updating, and test a copy of the database before using the upgrade with important records.
