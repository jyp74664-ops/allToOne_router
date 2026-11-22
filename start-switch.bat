@echo off
rem FlowGate startup script - uses the project venv (system python has no uvicorn)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Please create it first.
  exit /b 1
)
".venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8777
