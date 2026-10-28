@echo off
setlocal
cd /d "%~dp0"
docker compose -f compose.dev.yaml up --build -d --wait
if errorlevel 1 goto failed
start "" http://localhost:5173
echo Development: http://localhost:5173 - edit frontend/src or backend.
pause
exit /b 0
:failed
echo Development start failed. Check Docker Desktop and the output above.
pause
exit /b 1
