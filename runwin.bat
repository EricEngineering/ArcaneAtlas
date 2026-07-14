@echo off
setlocal EnableExtensions
REM ============================
REM ArcaneAtlas Windows Launcher
REM ============================

REM ======= USER OPTION =======
REM Set to 1 to enable logging, 0 to disable
set LOG_ENABLED=0
REM ===========================

REM Always run from this script's folder
pushd "%~dp0"

REM Prefer virtualenv's python if present
set "PY_EXE=.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

REM Logs folder lives beside this script
set "APPDIR=%CD%"
set "LOGDIR=%APPDIR%\logs"

REM Build a safe timestamp for the log file
if "%LOG_ENABLED%"=="1" (
  if not exist "%LOGDIR%" mkdir "%LOGDIR%" >NUL 2>&1
  for /f "tokens=1-3 delims=/.- " %%a in ("%date%") do (
    set "mm=%%a" & set "dd=%%b" & set "yyyy=%%c"
  )
  set "timestr=%time: =0%"
  set "timestr=%timestr::=-%"
  set "timestr=%timestr:.=%"
  set "LOGFILE=%LOGDIR%\run-%yyyy%%mm%%dd%_%timestr%.log"
)

taskkill /IM "ArcaneAtlas.exe" /F

echo Launching ArcaneAtlas...

if "%LOG_ENABLED%"=="1" (
  "%PY_EXE%" -X dev -u -m arcaneatlas 1>>"%LOGFILE%" 2>&1
) else (
  "%PY_EXE%" -m arcaneatlas
)

set "rc=%ERRORLEVEL%"
echo Exit code: %rc%
if "%LOG_ENABLED%"=="1" echo Log: %LOGFILE%
if not "%rc%"=="0" pause

popd
endlocal