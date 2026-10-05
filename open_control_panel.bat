@echo off
REM Opens the Trade Copier control panel in your web browser.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Virtual environment not found. Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m trade_copier ui -c config.yaml
pause
