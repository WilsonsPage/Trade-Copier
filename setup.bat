@echo off
REM One-time setup: creates a virtual environment and installs dependencies.
cd /d "%~dp0"
py -3 -m venv .venv || (echo Python not found. Install Python 3.11 64-bit from python.org & pause & exit /b 1)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml
echo.
echo Setup complete. Edit config.yaml, then run:  .venv\Scripts\python.exe -m trade_copier check
pause
