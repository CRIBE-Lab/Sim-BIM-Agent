@echo off
setlocal
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PYTHONPATH=
if "%~1"=="" (
  start "Sim-BIM Agent" /min "%ComSpec%" /d /s /c ""%~f0" --desktop"
  exit /b
)
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.ps1 first to install the Python environment.
  if "%~1"=="--desktop" pause
  exit /b 1
)
if "%~1"=="--desktop" (
  title Sim-BIM Agent
  echo Sim-BIM Agent is opening your workbench in the browser.
  echo Close this window to stop serving the workbench.
  "%~dp0.venv\Scripts\python.exe" -B "%~dp0sim_bim.py" workbench
) else (
  "%~dp0.venv\Scripts\python.exe" -B "%~dp0sim_bim.py" %*
)
set "SIM_BIM_EXIT=%ERRORLEVEL%"
if "%~1"=="--desktop" if not "%SIM_BIM_EXIT%"=="0" pause
exit /b %SIM_BIM_EXIT%
