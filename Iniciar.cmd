@echo off
setlocal
cd /d "%~dp0"
call "%~dp0python_path.cmd"
if not defined AION_REBUILD_PYTHON (
  echo Python 3.12 nao encontrado. Veja o LEIA-ME.
  pause
  exit /b 1
)
if defined AION_REBUILD_PYTHONW (
  start "" "%AION_REBUILD_PYTHONW%" -X utf8 -B "%~dp0app_macro.py"
  exit /b 0
)
"%AION_REBUILD_PYTHON%" -X utf8 -B app_macro.py
if errorlevel 1 pause
