@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto directory_error
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_linux.ps1" %*
set "JK808_EXIT=%errorlevel%"
if /I "%~1"=="-CheckOnly" goto finish
echo.
echo Startup ended. Read the message above before closing this window.
pause
:finish
popd
exit /b %JK808_EXIT%
:directory_error
echo Cannot access the program folder. Extract the full package to a local folder first.
if /I "%~1"=="-CheckOnly" exit /b 2
pause
exit /b 2
