@echo off
setlocal
cd /d "%~dp0"
if exist .env exit /b 0
powershell -NoProfile -Command "$taskBytes=New-Object byte[] 24; $taskRng=[Security.Cryptography.RandomNumberGenerator]::Create(); $taskRng.GetBytes($taskBytes); $taskRng.Dispose(); $taskPassword=[Convert]::ToBase64String($taskBytes); [IO.File]::WriteAllText((Join-Path (Get-Location) '.env'),('FINGUARD_PASSWORD='+$taskPassword+[Environment]::NewLine+'FINGUARD_ALLOWED_HOSTS=localhost,127.0.0.1'+[Environment]::NewLine),[Text.UTF8Encoding]::new($false))"
exit /b %ERRORLEVEL%
