@echo off
rem Startup script for running the FastAPI app with uvicorn
python -m uvicorn app:app --host 0.0.0.0 --port 8777