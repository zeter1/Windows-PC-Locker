# Windows PC Locker

A lightweight Windows utility that locks the current Windows session while allowing background tasks to continue running.

The application uses the native Windows lock screen through `LockWorkStation`. It does **not** implement a custom password screen and does **not** store Windows passwords or PINs.

## Features

- Native Windows session locking with `LockWorkStation`
- Optional prevention of automatic system sleep while the PC is locked
- Optional prevention of display power-off
- Configurable safety timer from 1 to 72 hours
- Automatic stop of keep-awake mode after the Windows session is unlocked
- Windows session-state monitoring through WTS APIs
- Retry handling for temporary `SetThreadExecutionState` failures
- Single-instance protection through a Windows mutex
- Persistent settings stored next to the application
- Optional diagnostic logging
- Configurable log retention from 1 to 120 days
- Rotating logs with strict size limits
- Compact `diagnostic_context.json` and `last_problem.json` files designed for troubleshooting with ChatGPT/Codex
- Automatic cleanup of old diagnostic files
- No third-party Python packages required

## Why this exists

When Windows is locked normally, power settings may still put the computer to sleep. That can interrupt long-running background work such as downloads, video processing, translation, rendering, backups or other automated jobs.

Windows PC Locker can periodically reset the Windows idle timers while the session is locked. When the configured timer expires — or when the user unlocks Windows with automatic stop enabled — the application stops sending keep-awake signals and normal Windows power settings take control again.

## Requirements

- Windows 10 or Windows 11
- Python 3.10 or newer

The program uses only the Python standard library and Windows APIs through `ctypes`.

## Running

```powershell
pythonw computer_locker.pyw
```

For troubleshooting from a console:

```powershell
python computer_locker.pyw
```

## Safe self-test

The project includes a non-destructive self-test intended for CI and diagnostics:

```powershell
python computer_locker.pyw --self-test
```

The self-test checks that the required Windows APIs are available and reports storage/logging configuration. It does not lock the PC and does not intentionally enable keep-awake mode.

## Power options

### Prevent the computer from sleeping

When enabled, the application periodically calls `SetThreadExecutionState` with `ES_SYSTEM_REQUIRED` while the safety timer is active.

### Keep the monitor on

This is optional and disabled by default. Background applications generally do not need the display to remain powered on. Enable it only when required for a specific system or workflow.

### Automatically allow sleep after unlock

When enabled, the application watches the current Windows session state. After it has observed the locked state and then detects that the session is unlocked, keep-awake mode is stopped automatically.

The normal Windows password/PIN screen is always responsible for authentication.

## Settings and diagnostics

Runtime data is deliberately stored next to the program:

```text
Windows-PC-Locker/
├─ computer_locker.pyw
├─ Настройки программы/
│  └─ settings.json
└─ Логи проблем/
   ├─ computer_locker.log
   ├─ computer_locker.log.1
   ├─ computer_locker.log.2
   ├─ diagnostic_context.json
   └─ last_problem.json
```

These runtime folders are ignored by Git and should not be committed.

### Diagnostic limits

The application is designed to keep diagnostic data compact:

- logging can be disabled completely;
- retention can be configured from 1 to 120 days;
- one rotating log file is limited to 256 KB;
- two rotated backups are retained;
- the entire diagnostic directory is limited to approximately 1 MB;
- `diagnostic_context.json` is overwritten instead of growing indefinitely;
- `last_problem.json` keeps only the latest significant problem;
- stored traceback text is length-limited;
- damaged settings backups are also cleaned up and limited in count.

If a problem occurs, the most useful files to provide for diagnosis are the contents of `Логи проблем`.

## Data and privacy

- The application does not store a separate unlock password.
- It does not replace the Windows lock screen.
- Authentication remains fully handled by Windows.
- No network connection or online service is required by the application itself.
- Settings and diagnostics remain in folders next to the program.

## GitHub Actions

The repository includes a Windows GitHub Actions workflow that performs:

1. Python syntax compilation of `computer_locker.pyw`.
2. The built-in safe `--self-test` on a Windows runner.

## Project status

Current application version: **2.3 SAFE**

The application has been manually tested on Windows for its normal lock/unlock workflow. GitHub Actions provides additional automated syntax and self-test verification, but it cannot reproduce every real desktop power-management configuration.
