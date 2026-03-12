@echo off
setlocal
uv run --no-project python "%~dp0scaffold_agent.py" certify %*
