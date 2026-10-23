@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "linux_temperature_test.py"
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 "linux_temperature_test.py"
) else (
  python "linux_temperature_test.py"
)
if errorlevel 1 pause
