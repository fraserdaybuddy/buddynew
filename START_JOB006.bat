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

:: ── Step 1: Daily pipeline (scrape + screen + ledger) ──────────────────────
echo [1/2] Running daily pipeline...
echo       This scrapes Betfair markets, screens for edge, writes bets to ledger.
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
echo  ========================================
echo.

:: ── Step 2: Start dashboard server in this window ─────────────────────────
echo [2/2] Starting API server on http://127.0.0.1:5000
echo       Keep this window OPEN. Close it to stop the server.
echo.

:: Open browser after short delay (background cmd)
start "" cmd /c "timeout /t 3 >nul && start http://127.0.0.1:5000"

python run_server.py

:: If server exits (shouldn't happen), pause so user sees any error
echo.
echo Server stopped. Press any key to exit.
pause >nul
