@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ========= Config =========
set "SPEC_FILE=arcaneatlas.spec"
set "EXE_NAME=ArcaneAtlas.exe"
set "DIST_DIR=dist\ArcaneAtlas"
set "RES_DIR=arcaneatlas\resources"
set "INSTALLER_DIR=installer"
set "ASSETS_DIR=%INSTALLER_DIR%\assets"
set "ISS_SRC=arcaneatlas.iss"
set "ISS_FILE=%INSTALLER_DIR%\arcaneatlas.iss"
REM ==========================

pushd "%~dp0"

echo.
echo [*] Cleaning previous build output (no prompts)...
if exist "%DIST_DIR%"  rmdir /S /Q "%DIST_DIR%"
if exist "build"       rmdir /S /Q "build"
if exist "__pycache__" rmdir /S /Q "__pycache__"

echo.
echo [*] Building with PyInstaller...
pyinstaller --noconfirm "%SPEC_FILE%"
if errorlevel 1 (
  echo [!] PyInstaller failed. Aborting.
  goto :end
)

if not exist "%DIST_DIR%\%EXE_NAME%" (
  echo [!] Built executable not found: "%DIST_DIR%\%EXE_NAME%"
  goto :end
)

echo.
echo [*] Preparing installer directory...
mkdir "%INSTALLER_DIR%" 2>nul

echo.
echo [*] Copying ISS to installer folder...
if not exist "%ISS_SRC%" (
  echo [!] Cannot find %ISS_SRC%
  goto :end
)
copy /Y "%ISS_SRC%" "%ISS_FILE%" >nul
echo   [+] Copied %ISS_SRC% to %ISS_FILE%

REM ---- Derive version from arcaneatlas\__init__.py (single source of truth) ----
set "VERSION="
for /f "tokens=2 delims== " %%v in ('findstr /b /c:"__version__" "arcaneatlas\__init__.py"') do set "VERSION=%%~v"
if not defined VERSION (
  echo [!] Could not read __version__ from arcaneatlas\__init__.py. Aborting.
  goto :end
)
echo   [+] Version from __init__.py: %VERSION%

REM ---- Locate ISCC ----
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo [!] ISCC.exe not found. Install Inno Setup or adjust the path.
  goto :end
)

echo.
echo [*] Compiling installer with Inno...
"%ISCC%" /DMyAppVersion=%VERSION% "%ISS_FILE%"
if errorlevel 1 (
  echo [!] Inno compilation failed.
  goto :end
)

echo.
echo [*] Build complete. Look for the installer in "%INSTALLER_DIR%\output"
if exist "%INSTALLER_DIR%\output" dir /b "%INSTALLER_DIR%\output"

goto :end

:copy_one
set "SRC=%~1"
set "DST=%~2"
if exist "%SRC%" (
  copy /Y "%SRC%" "%DST%" >nul
  echo   [+] %~nx1 -> %~nx2
) else (
  echo   [!] Missing asset: %SRC%
)
exit /b 0

:end
popd
endlocal