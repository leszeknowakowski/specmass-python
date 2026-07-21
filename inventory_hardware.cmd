@echo off
setlocal

if "%~1"=="" (
    echo Usage: inventory_hardware.cmd "C:\path\to\Builds"
    echo.
    echo This command only reads configuration and enumerates COM ports.
    echo It does not open a port or send a device command.
    exit /b 2
)

set "SPECMASS_DIR=%~dp0"
set "PYTHONPATH=%SPECMASS_DIR%src"
set "PYTHON_EXE=python"
set "VENV_PYTHON=%SPECMASS_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" goto run_inventory

"%VENV_PYTHON%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo Ignoring copied or broken .venv; using Python from PATH.
    goto run_inventory
)
set "PYTHON_EXE=%VENV_PYTHON%"

:run_inventory
"%PYTHON_EXE%" -m specmass.hardware_inventory --builds "%~f1" --output "%SPECMASS_DIR%specmass-hardware-inventory.json"
if errorlevel 1 exit /b %errorlevel%

echo.
echo Send this file back to Codex:
echo %SPECMASS_DIR%specmass-hardware-inventory.json
