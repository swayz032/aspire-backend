@echo off
setlocal
set VENV=%~dp0..\safety-gateway\.venv313\Scripts\python.exe
if not exist "%VENV%" (
  echo Safety Gateway venv not found at %VENV%
  echo Run: cmd /c scripts\bootstrap_safety_gateway.cmd
  exit /b 1
)
"%VENV%" -m uvicorn aspire_safety_gateway.app:app --host 0.0.0.0 --port 8787 %*
