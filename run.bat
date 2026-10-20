@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" goto :missing_python

start "" /b ".venv\Scripts\pythonw.exe" -m app.main
exit /b 0

:missing_python
echo ERROR: .venv\Scripts\pythonw.exe was not found.
echo Run the installer first, then start this file again.
pause
exit /b 1
