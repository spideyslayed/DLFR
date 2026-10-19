@echo off
setlocal
cd /d "%~dp0"
echo ===========================================
echo DLFR - Backend startup
echo ===========================================
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment...
  py -m venv .venv
  if errorlevel 1 goto failed
)
call ".venv\Scripts\activate.bat"
if not exist ".venv\dlfr-deps-installed.txt" (
  echo Installing Python dependencies; this may take a few minutes...
  python -m pip install --upgrade pip
  if errorlevel 1 goto failed
  python -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo installed> ".venv\dlfr-deps-installed.txt"
)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created backend\.env from the example.
  echo For a brand-new database, edit OWNER_SETUP_KEY before creating the owner account.
)
echo Starting API at http://127.0.0.1:8000
echo API docs: http://127.0.0.1:8000/docs
python -m uvicorn app.main:app --reload --port 8000
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. Read the error above, check internet access for dependency installation, and try again.
pause
exit /b 1
