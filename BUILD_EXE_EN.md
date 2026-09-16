**Язык / Language:** [Русский](BUILD_EXE.md) · **English**

# Building Windows PC Locker as EXE

`build_exe.bat` is the only command a user needs to start manually. Double-clicking the BAT runs the complete verified build pipeline and publishes `dist/` only after the packaged executable passes all required checks.

## Easiest way

1. Download the repository and extract it to a normal writable folder.
2. Double-click `build_exe.bat`.
3. Wait for `[OK] READY BUILD CREATED AND VERIFIED`.
4. Use the files from `dist/`.

The finished distribution contains:

```text
dist/
├─ Windows-PC-Locker.exe
├─ Windows-PC-Locker-portable-x64.zip
├─ build_info.json
└─ SHA256SUMS.txt
```

Python and PyInstaller are not required on the target Windows PC.

## What build_exe.bat does

The BAT is intentionally a small entry point and delegates reliable build work to `tools/build_windows.ps1`. The builder:

1. checks Windows, architecture, write access and at least 2 GB of free space;
2. locates Python 3.13 x64;
3. if Python is missing in a normal local build, first tries `winget`, then falls back to the official Python 3.13.15 installer with mandatory SHA-256 verification;
4. creates an isolated `.build-venv` instead of modifying the user's normal Python installation;
5. installs the pinned packaging toolchain from `requirements-build.txt`;
6. runs `pip check`;
7. compiles `computer_locker.pyw`;
8. runs the safe source `--self-test`;
9. generates a Windows manifest with `asInvoker`, DPI awareness and long-path awareness;
10. embeds Windows FileVersion/ProductVersion/ProductName metadata;
11. builds a `onefile` executable with PyInstaller and no UPX;
12. rejects an implausibly small packaged executable;
13. runs the packaged `--self-test` with a hard 60-second timeout;
14. copies the EXE to an isolated temporary path containing spaces and Cyrillic and tests it again;
15. creates a portable ZIP;
16. extracts that ZIP to another clean temporary directory, verifies the EXE SHA-256 and runs the packaged self-test again;
17. writes `build_info.json` and `SHA256SUMS.txt`;
18. atomically replaces `dist/` only after all checks succeed.

If a new candidate build fails before publication, the existing working `dist/` is left untouched.

## Previous good build

Before publishing a newly verified distribution, the old `dist/` is moved to:

```text
dist_previous/
```

If final publication fails, the builder attempts to restore the previous `dist/` automatically.

## Modes

### Normal verified build

```bat
build_exe.bat
```

Recommended before distributing the application.

### Fast rebuild

```bat
build_exe.bat --fast
```

Reuses a compatible build environment and PyInstaller work cache. Critical packaged, portable-folder and ZIP tests still run.

### Clean rebuild

```bat
build_exe.bat --clean
```

Recreates the build environment and caches from scratch. The current working `dist/` is still preserved until the new build has fully passed verification.

### Diagnose only

```bat
build_exe.bat --diagnose
```

Checks Windows, architecture, free space, write access and compatible Python without installing dependencies or building an EXE.

### CI mode

```bat
build_exe.bat --ci
```

Designed for GitHub Actions/Codex: no pause, meaningful exit codes and no global Python installation. CI must provide Python through `actions/setup-python`.

## Build logs

Detailed transcript:

```text
build_logs/build_YYYYMMDD_HHMMSS.log
```

Compact machine-readable summary for ChatGPT/Codex:

```text
build_logs/last_build_summary.json
```

The summary records stage, status, error, Python, PyInstaller, commit and artifact path. At most 20 detailed build logs are retained.

## build_info.json

The file next to the executable records:

- application version;
- Git commit SHA when built from a Git checkout;
- Python version;
- PyInstaller version;
- architecture;
- EXE SHA-256;
- verification levels that actually ran.

Builds made from a source ZIP without `.git` use `source-archive` as the commit value.

## SHA256SUMS.txt

Contains SHA-256 hashes for the EXE and portable ZIP, allowing the final files to be checked for modification or corruption.

## Why staging is used

PyInstaller first produces a candidate under `.build/`. `dist/` is not considered a result until the candidate passes source verification, packaged verification, isolated portable-folder verification and ZIP extraction verification.

Merely creating an `.exe` is therefore not treated as a successful build.

## Safe to delete

These build-only directories can be removed at any time:

```text
.build/
.build-cache/
.build-venv/
build_logs/
dist_previous/
```

They will be recreated when needed. To run the finished application, only the contents of `dist/` are needed; for distribution, `Windows-PC-Locker-portable-x64.zip` is usually the most convenient artifact.

## If a build fails

Check, in order:

1. the first `[ERROR]` shown in the console;
2. `build_logs/last_build_summary.json`;
3. the newest `build_logs/build_*.log`.

Common causes include a proxy/antivirus blocking `pip` or python.org, insufficient free space, lack of write permission in the project directory, or antivirus temporarily blocking a newly created unsigned EXE.

The builder intentionally does not suppress these failures and never reports an unverified executable as ready.
