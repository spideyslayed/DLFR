@echo off
setlocal
cd /d "%~dp0"
if not exist "dlfr.db" (
  echo No dlfr.db found in this folder. Start the backend first to create the database.
  pause
  exit /b 1
)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set stamp=%%i
copy /y "dlfr.db" "dlfr-backup-%stamp%.db" >nul
if errorlevel 1 (
  echo Backup failed.
  pause
  exit /b 1
)
echo Database backup created: dlfr-backup-%stamp%.db
pause
