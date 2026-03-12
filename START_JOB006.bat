@echo off
title JOB-006 Sports Betting System
cd /d "C:\Users\frase\Downloads\Claude\JOB006_complete_v2\sports-betting"
set PYTHONUTF8=1

echo.
echo  ========================================
echo   JOB-006 Sports Betting System
echo   %DATE%  %TIME%
echo  ========================================
echo.

:: ── Register as Windows startup item (runs once, silently skips if already done) ──
set TASK_NAME=JOB006_AutoStart
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% neq 0 (
  echo [setup] Registering auto-start task in Windows Task Scheduler...
  schtasks /create /tn "%TASK_NAME%" /tr "cmd /c \"cd /d C:\Users\frase\Downloads\Claude\JOB006_complete_v2\sports-betting && set PYTHONUTF8=1 && python run_server.py\"" /sc onlogon /rl highest /f >nul 2>&1
  if %errorlevel% equ 0 (
    echo [setup] Auto-start registered. Server will launch automatically at Windows login.
  ) else (
    echo [setup] Could not register auto-start ^(run as Administrator to enable^).
  )
  echo.
)

:: ── Step 1: Daily pipeline (scrape + screen + ledger) ──────────────────────
echo [1/2] Running daily pipeline...
echo       Scrapes Betfair markets, screens for edge, shows signals.
echo       The server will re-scrape automatically every 2h after this.
echo.
python run_daily.py
if %errorlevel% neq 0 (
  echo.
  echo  [!] Daily pipeline reported errors - check output above.
  echo      You can still start the dashboard and settle pending bets.
  echo.
  pause
)

echo.
echo  ========================================
echo   Daily pipeline complete.
echo   Starting dashboard server...
echo   Server will auto-backup daily and re-scrape every 2h.
echo   Keep this window OPEN.
echo  ========================================
echo.

:: ── Step 2: Start dashboard server ─────────────────────────────────────────
echo [2/2] Starting API server on http://127.0.0.1:5000
echo.

:: Open browser after short delay
start "" cmd /c "timeout /t 3 >nul && start http://127.0.0.1:5000"

python run_server.py

:: If server exits (shouldn't happen), pause so user sees any error
echo.
echo Server stopped. Press any key to exit.
pause >nul
