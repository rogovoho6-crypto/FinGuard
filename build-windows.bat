@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%~dp0.venv\Scripts"
if not exist ".venv\Scripts\python.exe" goto failed
if not exist "frontend\dist\index.html" goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m unittest discover -s backend\tests -v
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --noupx --name FinGuard-Web --collect-submodules uvicorn --add-data "%~dp0frontend\dist;frontend/dist" --icon "%~dp0assets\finguard.ico" --distpath dist --workpath build --specpath build "%~dp0run.py"
if errorlevel 1 goto failed
echo Built: dist\FinGuard-Web.exe
pause
exit /b 0
:failed
echo Build failed. Read docs\DEVELOPMENT.md and build the frontend first.
pause
exit /b 1
