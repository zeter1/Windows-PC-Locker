@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PAUSE="
if /i "%~1"=="--ci" set "NO_PAUSE=1"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"

set "PY_CMD="
set "PY_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARGS=-3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo [ERROR] Python 3 was not found.
    echo Install Python 3.10+ from python.org and enable the Python launcher or PATH.
    goto :fail
)

set "BUILD_VENV=%CD%\.build-venv"
set "BUILD_PY=%BUILD_VENV%\Scripts\python.exe"

if not exist "%BUILD_PY%" (
    echo [1/5] Creating isolated build environment...
    %PY_CMD% %PY_ARGS% -m venv "%BUILD_VENV%" || goto :fail
)

echo [2/5] Installing build tools...
"%BUILD_PY%" -m pip install --upgrade pip setuptools wheel || goto :fail
"%BUILD_PY%" -m pip install "pyinstaller>=6.22,<7" || goto :fail

echo [3/5] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "Windows-PC-Locker.spec" del /q "Windows-PC-Locker.spec"

echo [4/5] Building Windows-PC-Locker.exe...
"%BUILD_PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "Windows-PC-Locker" ^
  computer_locker.pyw || goto :fail

if not exist "dist\Windows-PC-Locker.exe" (
    echo [ERROR] Expected EXE was not created.
    goto :fail
)

echo [5/5] Running packaged safe self-test...
"dist\Windows-PC-Locker.exe" --self-test || goto :fail

echo.
echo [OK] Build and packaged self-test completed successfully.
echo EXE: %CD%\dist\Windows-PC-Locker.exe
goto :success

:fail
echo.
echo [ERROR] EXE build failed. Review the messages above.
if not defined NO_PAUSE pause
exit /b 1

:success
if not defined NO_PAUSE pause
exit /b 0
