@echo off
REM Starts the trade copier using the virtual environment in this folder.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Virtual environment not found. Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m trade_copier run -c config.yaml
pause
