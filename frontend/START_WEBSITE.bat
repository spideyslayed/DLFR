@echo off
setlocal
cd /d "%~dp0"
echo ===========================================
echo DLFR - Website startup
echo ===========================================
if not exist "node_modules" (
  echo Installing website packages; this may take a few minutes...
  call npm install
  if errorlevel 1 goto failed
)
echo Starting website; open the Local URL printed below.
call npm run dev
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Website startup failed. Make sure Node.js 20+ and npm are installed and that internet is available for first-time package installation.
pause
exit /b 1
