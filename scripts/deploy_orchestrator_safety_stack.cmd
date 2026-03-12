@echo off
setlocal
set SCRIPT_DIR=%~dp0
set ROOT_DIR=%SCRIPT_DIR%..

if exist "%ROOT_DIR%\orchestrator\.venv\Scripts\python.exe" (
  "%ROOT_DIR%\orchestrator\.venv\Scripts\python.exe" "%SCRIPT_DIR%deploy_orchestrator_safety_stack.py" %*
) else (
  python "%SCRIPT_DIR%deploy_orchestrator_safety_stack.py" %*
)
