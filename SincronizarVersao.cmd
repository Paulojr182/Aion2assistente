@echo off
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)
cd /d "%~dp0"
call "%~dp0python_path.cmd"
"%AION_REBUILD_PYTHON%" -X utf8 -B sincronizar_versao.py
echo.
pause
