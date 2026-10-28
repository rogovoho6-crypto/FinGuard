@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist "dist\FinGuard-Web.exe" (
  "dist\FinGuard-Web.exe"
  if errorlevel 1 pause
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist "frontend\dist\index.html" (
  echo Frontend build is missing. Follow docs\DEVELOPMENT.md to build it first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo FinGuard Web could not start. See the message above.
pause
exit /b 1
