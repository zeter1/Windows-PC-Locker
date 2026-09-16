@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================================
rem Windows PC Locker - user-friendly Windows build entrypoint
rem
rem Double-click this file for the normal verified build.
rem The real build logic lives in tools\build_windows.ps1 so it can provide
rem reliable downloads, hashes, timeouts, staging, ZIP verification and logs.
rem
rem Modes:
rem   build_exe.bat              full verified build
rem   build_exe.bat --fast       reuse caches / PyInstaller work where safe
rem   build_exe.bat --clean      recreate build environment and caches
rem   build_exe.bat --diagnose   inspect the build environment without building
rem   build_exe.bat --ci         non-interactive CI mode (never installs Python)
rem
rem Detailed guide: BUILD_EXE.md
rem ============================================================================

set "NO_PAUSE="
set "PS_ARGS="

:parse_args
if "%~1"=="" goto run_build
if /i "%~1"=="--ci" (
    set "NO_PAUSE=1"
    set "PS_ARGS=!PS_ARGS! -Ci"
) else if /i "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
) else if /i "%~1"=="--fast" (
    set "PS_ARGS=!PS_ARGS! -Fast"
) else if /i "%~1"=="--clean" (
    set "PS_ARGS=!PS_ARGS! -Clean"
) else if /i "%~1"=="--diagnose" (
    set "PS_ARGS=!PS_ARGS! -Diagnose"
) else (
    echo [ERROR] Unknown option: %~1
    echo See BUILD_EXE.md for supported modes.
    goto fail
)
shift
goto parse_args

:run_build
where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Windows PowerShell was not found.
    echo Windows 10/11 normally includes it. Repair Windows PowerShell and retry.
    goto fail
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_windows.ps1" !PS_ARGS!
set "BUILD_EXIT=!ERRORLEVEL!"
if not "!BUILD_EXIT!"=="0" goto fail_code

echo.
echo [OK] Verified distribution is ready in:
echo     %CD%\dist
echo See BUILD_EXE.md for the exact files and verification contract.
goto success

:fail_code
echo.
echo [ERROR] Build failed with exit code !BUILD_EXIT!.
echo Check build_logs\last_build_summary.json and the newest build_*.log.
if not defined NO_PAUSE pause
exit /b !BUILD_EXIT!

:fail
echo.
echo [ERROR] Build was not started successfully.
if not defined NO_PAUSE pause
exit /b 1

:success
if not defined NO_PAUSE pause
exit /b 0
