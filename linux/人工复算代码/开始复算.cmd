@echo off
chcp 65001 >nul
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3.12 "%~dp0复算温度.py" %*
goto finished
:use_python
python "%~dp0复算温度.py" %*
:finished
if errorlevel 1 echo 复算没有完成，请查看上面的原因。
pause
