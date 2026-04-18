@echo off
REM Always run from this repo root (fixes "python.exe not recognized" when cwd is wrong).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [X] Missing .venv\Scripts\python.exe — create venv: py -m venv .venv ^& .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0scripts\start_live_arb.py" %*
