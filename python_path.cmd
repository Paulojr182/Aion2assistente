@echo off
rem Descobre o Python do bot: 1) embutido no pacote (.\python) 2) Python 3.12 instalado 3) py launcher.
set "AION_REBUILD_PYTHON="
if exist "%~dp0python\python.exe" set "AION_REBUILD_PYTHON=%~dp0python\python.exe"
if not defined AION_REBUILD_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "AION_REBUILD_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined AION_REBUILD_PYTHON for /f "delims=" %%P in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "AION_REBUILD_PYTHON=%%P"
set "AION_REBUILD_PYTHONW="
if defined AION_REBUILD_PYTHON for %%D in ("%AION_REBUILD_PYTHON%") do if exist "%%~dpDpythonw.exe" set "AION_REBUILD_PYTHONW=%%~dpDpythonw.exe"
