**Язык / Language:** [Русский](README.md) · **English**

# Windows PC Locker

**Windows PC Locker** is a lightweight Windows system utility that locks the current session using the standard `LockWorkStation` mechanism and can keep the computer awake while long-running background tasks continue.

The application does not implement its own password screen, store PIN codes, or replace Windows security. Authentication after locking is handled by the standard Windows lock screen.

## Download and run on Windows

> Windows builds are marked **Prerelease**: they are preliminary builds for testing. A release build may differ from the current source.

1. Open [GitHub Releases](https://github.com/zeter1/Windows-PC-Locker/releases) and expand **Assets** for the build you choose.
2. Download `Windows-PC-Locker.exe` or extract `Windows-PC-Locker-portable-x64.zip`.
3. Place the program in a folder you can write to and run `Windows-PC-Locker.exe`.

The EXE includes Python. Settings and logs are stored beside the program. Unlocking uses standard Windows authentication. `SHA256SUMS.txt` is published beside the files. The CI self-test does not lock the machine or verify an interactive lock/unlock session.

## What the project demonstrates

- direct WinAPI integration through `ctypes` without third-party runtime dependencies;
- use of `LockWorkStation`, WTS API, and `SetThreadExecutionState`;
- correct separation between Windows session locking and power management;
- single-instance protection through a Windows mutex;
- a safe self-test that checks system integrations without locking the computer;
- compact diagnostics with strict log-size limits;
- design of a system utility with minimal privileges and no user-credential storage.

## Features

- standard session locking through `LockWorkStation`;
- prevention of automatic sleep while the protection mode is active;
- optional prevention of monitor power-off;
- safety timer from 1 to 72 hours;
- optional automatic release of sleep prevention after unlock;
- session-state tracking through WTS API;
- retry for temporary `SetThreadExecutionState` failures;
- single-instance protection through a Windows mutex;
- persistent settings;
- optional diagnostic logging;
- log rotation and size limits;
- compact `diagnostic_context.json` and `last_problem.json` artifacts;
- no third-party Python dependencies.

## Why it exists

Locking Windows does not guarantee that the machine will remain awake according to its current power plan. Sleep can interrupt downloads, video processing or translation, rendering, backups, computation, and other background tasks.

Windows PC Locker allows the session to be locked while temporarily keeping the system active without bypassing standard Windows authentication.

## Installation from source

The application uses only the Python standard library and Windows APIs through `ctypes`.

1. Install Python 3.10 or newer for Windows.
2. Download the repository with **Code → Download ZIP** or Git:

```bash
git clone https://github.com/zeter1/Windows-PC-Locker.git
cd Windows-PC-Locker
```

No additional Python packages are required.

## Launch from source

Without a console window:

```powershell
pythonw computer_locker.pyw
```

With a visible console for diagnostics:

```powershell
python computer_locker.pyw
```

## Build EXE

Run the repository build helper:

```text
build_exe.bat
```

The script creates an isolated `.build-venv`, updates the build tooling, installs a compatible PyInstaller version, removes stale build artifacts, builds the application, and runs the **safe self-test against the packaged executable itself**. It does not install build packages into the project's global Python environment.

Output:

```text
dist\Windows-PC-Locker.exe
```

This is a `onefile` build, so the target PC does not need Python installed. By application design, settings and diagnostics are stored next to the executable; keep the EXE in a directory where the user has write access.

## Usage

1. Start the application.
2. Enable **prevent sleep** if needed.
3. Optionally configure a safety timer, for example for a long download or video-processing task.
4. Enable **keep monitor on** only when the display itself must remain active.
5. Lock the computer.
6. Windows shows the standard lock screen while background tasks continue.
7. After unlock, the application can restore normal power behavior automatically if that option is enabled.

The application does not know the user's password or PIN and does not participate in authentication.

## Power-management modes

### Prevent sleep

The application periodically calls `SetThreadExecutionState` with `ES_SYSTEM_REQUIRED` while the protection mode is active.

### Keep monitor on

An additional mode for scenarios where the display also needs to stay powered. Most background tasks do not require it.

### Allow sleep after unlock

The application tracks Windows session state. After a lock → unlock sequence, activity preservation can be released automatically.

## Safe self-test

```powershell
python computer_locker.pyw --self-test
```

The self-test checks the required Windows API availability and diagnostic configuration, while deliberately avoiding workstation locking or enabling sleep prevention.

## Data and diagnostics

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

Diagnostics are intentionally compact: logging can be disabled, retention is configurable, log size and backup count are bounded, and `diagnostic_context.json` plus `last_problem.json` preserve focused context for troubleshooting.

## Privacy

- the application does not store the Windows password;
- it does not replace the standard lock screen;
- it does not require an Internet connection;
- settings and diagnostics remain local.

## Verification and GitHub Actions

Local baseline verification:

```powershell
python -m py_compile computer_locker.pyw
python computer_locker.pyw --self-test
```

GitHub Actions runs the same safe checks on a Windows runner. For packaging changes, CI additionally runs `build_exe.bat --ci` and executes the packaged EXE self-test. CI does not verify a real lock/unlock cycle in an interactive user session.

## Support and security

- [`SUPPORT.md`](SUPPORT.md) — data for a reproducible bug report;
- [GitHub Issues](https://github.com/zeter1/Windows-PC-Locker/issues) — regular bugs;
- [`SECURITY.md`](SECURITY.md) — potential security vulnerabilities.

## Version

Current version: **2.3 SAFE**.

## License

No open-source license is currently granted. The source code is published for portfolio review and code review.

## Developer contacts

**Язык / Language:** [Русский](README.md#контакты-для-связи-с-разработчиком) · **English**

- [zeter11@gmail.com](mailto:zeter11@gmail.com)
- [https://t.me/zeter1](https://t.me/zeter1)
- [https://www.linkedin.com/in/zeter/](https://www.linkedin.com/in/zeter/)
- [https://www.facebook.com/zeter1](https://www.facebook.com/zeter1)
- [https://vk.ru/zeter1](https://vk.ru/zeter1)
- [https://www.instagram.com/zeter1992/](https://www.instagram.com/zeter1992/)
- [https://tenchat.ru/zeter](https://tenchat.ru/zeter)
- [https://x.com/zeter1](https://x.com/zeter1)
- [https://www.threads.com/@zeter1992](https://www.threads.com/@zeter1992)
- [https://ok.ru/zeter1](https://ok.ru/zeter1)
- [https://t.me/zeterchat](https://t.me/zeterchat) — Telegram group: about me, projects and chat
