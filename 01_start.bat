@echo off
setlocal
cd /d "%~dp0"
call 00_prepare.bat
if errorlevel 1 goto failed
docker compose up --build -d --wait
if errorlevel 1 goto failed
echo Login: finguard. Password: FINGUARD_PASSWORD in .env
start "" http://localhost:8765
pause
exit /b 0
:failed
echo Start failed. Open Docker Desktop and read the message above.
pause
exit /b 1
