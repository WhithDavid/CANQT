@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :missing_python

".venv\Scripts\python.exe" build_exe.py
if errorlevel 1 goto :build_failed

echo.
echo Build completed: dist\CANQT\CANQT.exe
pause
exit /b 0

:missing_python
echo ERROR: .venv\Scripts\python.exe was not found.
pause
exit /b 1

:build_failed
echo.
echo ERROR: CANQT build failed.
pause
exit /b 1
