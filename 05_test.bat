@echo off
setlocal
cd /d "%~dp0"
docker compose -f compose.test.yaml build
if errorlevel 1 goto failed
docker compose -f compose.test.yaml run --rm tests
if errorlevel 1 goto failed
echo All container tests passed.
pause
exit /b 0
:failed
echo Tests or image build failed. Read the error above.
pause
exit /b 1
