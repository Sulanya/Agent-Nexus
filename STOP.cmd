@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Follow README to create .venv first. & exit /b 1)
".venv\Scripts\python.exe" -X utf8 tools\dev.py stop %*
