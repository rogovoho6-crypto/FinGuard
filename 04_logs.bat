@echo off
setlocal
cd /d "%~dp0"
docker compose logs --tail 80
docker compose -f compose.dev.yaml logs --tail 80
pause
