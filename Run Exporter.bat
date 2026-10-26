@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Steam Library JSON Exporter
pushd "%~dp0" || exit /b 1

if defined STEAM_EXPORTER_PYTHON (
    "%STEAM_EXPORTER_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 goto custom_python
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_launcher
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_path

echo Python 3.10 or newer is required.
echo Install it from https://www.python.org/downloads/ and enable Add Python to PATH.
echo Then double-click this file again.
set "result=1"
goto finish

:custom_python
"%STEAM_EXPORTER_PYTHON%" "%~dp0steam_exporter.py" --interactive %*
set "result=%errorlevel%"
goto finish

:python_launcher
py -3 "%~dp0steam_exporter.py" --interactive %*
set "result=%errorlevel%"
goto finish

:python_path
python "%~dp0steam_exporter.py" --interactive %*
set "result=%errorlevel%"

:finish
echo.
if "%result%"=="0" echo Export completed. See the saved file path above.
if not "%result%"=="0" echo Export did not complete successfully. See the message above.
pause
popd
endlocal & exit /b %result%
