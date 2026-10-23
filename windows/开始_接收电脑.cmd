@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0runtime\python.exe" -B "%~dp0启动校验.py"
if errorlevel 1 goto end
"%~dp0runtime\python.exe" -B -u "%~dp0集中联调.py" 2
:end
pause
