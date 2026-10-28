@echo off
setlocal
cd /d "%~dp0"
docker compose stop
docker compose -f compose.dev.yaml stop
echo Stopped. Databases are preserved in Docker volumes.
pause
