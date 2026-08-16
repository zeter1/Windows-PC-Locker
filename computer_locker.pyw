# -*- coding: utf-8 -*-
"""
Безопасная блокировка ПК для Windows.

Что делает программа:
- блокирует Windows штатной функцией LockWorkStation;
- НЕ создаёт собственный экран блокировки и НЕ хранит пароль;
- при необходимости периодически сбрасывает таймер сна Windows, чтобы
  фоновые программы продолжали работать;
- опционально не даёт монитору автоматически погаснуть;
- ограничивает этот режим защитным таймером;
- умеет автоматически прекращать поддержание активности после разблокировки;
- хранит настройки и логи только рядом с программой;
- умеет полностью отключать запись логов проблем;
- позволяет выбрать срок хранения логов от 1 до 120 дней;
- ограничивает размер логов, чтобы папка не разрасталась.

Для разблокировки используется обычный пароль / PIN Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from typing import Any


APP_NAME = "Безопасная блокировка ПК"
APP_VERSION = "2.3 SAFE"
APP_FOLDER = "ComputerLockerSafe"

if sys.platform != "win32":
    raise SystemExit("Эта программа предназначена только для Windows.")


# ---------------------------------------------------------------------------
# Windows API
# ---------------------------------------------------------------------------

# Без ES_CONTINUOUS вызов SetThreadExecutionState только сбрасывает таймеры
# простоя. Поэтому программа повторяет вызов раз в KEEP_ALIVE_INTERVAL_SEC.
# После остановки программы не остаётся постоянного execution state.
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

ERROR_ALREADY_EXISTS = 183
KEEP_ALIVE_INTERVAL_SEC = 30
KEEP_ALIVE_RETRY_COUNT = 3
KEEP_ALIVE_RETRY_DELAY_SEC = 1.0
SESSION_POLL_INTERVAL_SEC = 1.0
SESSION_QUERY_MAX_ERRORS = 3

# WTSQuerySessionInformationW constants.
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTSSessionInfoEx = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1
WTS_SESSIONSTATE_UNKNOWN = 0xFFFFFFFF

WINSTATIONNAME_LENGTH = 32
USERNAME_LENGTH = 20
DOMAIN_LENGTH = 17

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD

kernel32.CreateMutexW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

user32.LockWorkStation.argtypes = []
user32.LockWorkStation.restype = wintypes.BOOL

user32.MessageBoxW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.UINT,
]
user32.MessageBoxW.restype = ctypes.c_int

# Старый API достаточно безопасен как best-effort улучшение чёткости Tkinter.
try:
    user32.SetProcessDPIAware.argtypes = []
    user32.SetProcessDPIAware.restype = wintypes.BOOL
except AttributeError:
    pass

wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD),
]
wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL

wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
wtsapi32.WTSFreeMemory.restype = None


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * (WINSTATIONNAME_LENGTH + 1)),
        ("UserName", wintypes.WCHAR * (USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class WTSINFOEX_LEVEL_W(ctypes.Union):
    _fields_ = [("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W)]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [
        ("Level", wintypes.DWORD),
        ("Data", WTSINFOEX_LEVEL_W),
    ]


def _is_windows_7() -> bool:
    try:
        version = sys.getwindowsversion()
        return version.major == 6 and version.minor == 1
    except Exception:
        return False


def _query_current_session_locked() -> bool | None:
    """
    Возвращает:
    - True  -> текущий сеанс заблокирован;
    - False -> текущий сеанс разблокирован;
    - None  -> WTS вернул неизвестное состояние.

    На Windows 7 Microsoft документирует обратные значения lock/unlock,
    поэтому для этой версии они инвертируются.
    """
    buffer = ctypes.c_void_p()
    bytes_returned = wintypes.DWORD(0)

    ctypes.set_last_error(0)
    ok = wtsapi32.WTSQuerySessionInformationW(
        None,
        WTS_CURRENT_SESSION,
        WTSSessionInfoEx,
        ctypes.byref(buffer),
        ctypes.byref(bytes_returned),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        if not buffer.value:
            raise RuntimeError("WTSQuerySessionInformationW вернул пустой буфер.")

        minimum = ctypes.sizeof(WTSINFOEXW)
        if bytes_returned.value < minimum:
            raise RuntimeError(
                "WTSQuerySessionInformationW вернул слишком короткий буфер: "
                f"{bytes_returned.value} < {minimum}."
            )

        info = ctypes.cast(
            buffer,
            ctypes.POINTER(WTSINFOEXW),
        ).contents

        if info.Level != 1:
            return None

        flags = int(info.Data.WTSInfoExLevel1.SessionFlags)

        if flags == WTS_SESSIONSTATE_UNKNOWN:
            return None

        if _is_windows_7():
            if flags == WTS_SESSIONSTATE_LOCK:
                return False
            if flags == WTS_SESSIONSTATE_UNLOCK:
                return True
        else:
            if flags == WTS_SESSIONSTATE_LOCK:
                return True
            if flags == WTS_SESSIONSTATE_UNLOCK:
                return False

        return None

    finally:
        wtsapi32.WTSFreeMemory(buffer)


# ---------------------------------------------------------------------------
# Storage / logs
# ---------------------------------------------------------------------------

# Все пользовательские данные хранятся РЯДОМ С ПРОГРАММОЙ.
# Никакого %LOCALAPPDATA% и скрытого переноса в другой каталог нет.
DEFAULT_LOG_RETENTION_DAYS = 120
MIN_LOG_RETENTION_DAYS = 1
MAX_LOG_RETENTION_DAYS = 120
MAX_LOG_FILE_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 2
MAX_LOG_DIR_BYTES = 1 * 1024 * 1024
MAX_BROKEN_SETTINGS_BACKUPS = 5
MAX_PROBLEM_TRACE_CHARS = 6000


def _program_dir() -> Path:
    """
    Папка, где лежит запущенный .pyw или собранный .exe.

    Для PyInstaller нельзя использовать __file__: в one-file сборке он указывает
    во временную распакованную папку. Поэтому у frozen-приложения берём sys.executable.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable)
    else:
        base = Path(__file__)

    try:
        return base.resolve().parent
    except OSError:
        return base.absolute().parent


PROGRAM_DIR = _program_dir()
DATA_DIR = PROGRAM_DIR
SETTINGS_DIR = PROGRAM_DIR / "Настройки программы"
LOG_DIR = PROGRAM_DIR / "Логи проблем"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"

STORAGE_INIT_ERROR: Exception | None = None


def _prepare_storage() -> None:
    """
    Создаёт локальные папки и сразу проверяет, что они доступны для записи.

    Если программа лежит, например, в защищённой папке Program Files без прав
    на запись, мы не переносим данные молча в другое место: пользователь
    получает понятную ошибку и может переместить программу в обычную папку.
    """
    global STORAGE_INIT_ERROR

    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        for folder in (SETTINGS_DIR, LOG_DIR):
            probe = folder / ".write_test.tmp"
            try:
                with probe.open("wb") as stream:
                    stream.write(b"ok")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                try:
                    probe.unlink()
                except FileNotFoundError:
                    pass

    except Exception as exc:
        STORAGE_INIT_ERROR = exc


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _normalize_log_retention_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = DEFAULT_LOG_RETENTION_DAYS
    return min(MAX_LOG_RETENTION_DAYS, max(MIN_LOG_RETENTION_DAYS, days))


def _read_bootstrap_logging_preferences() -> tuple[bool, int]:
    """
    Читает только настройки логирования до запуска GUI.

    Если settings.json повреждён, здесь он не переименовывается: полноценная
    загрузка настроек позже сохранит повреждённый файл для диагностики.
    """
    enabled = True
    retention_days = DEFAULT_LOG_RETENTION_DAYS

    try:
        if SETTINGS_PATH.exists():
            with SETTINGS_PATH.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                enabled = bool(loaded.get("write_logs", True))
                retention_days = _normalize_log_retention_days(
                    loaded.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)
                )
    except Exception:
        pass

    return enabled, retention_days


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _cleanup_old_diagnostics(retention_days: int) -> tuple[int, int]:
    """
    Ограничивает диагностические файлы по возрасту И по общему размеру.

    Срок задаёт пользователь, но он всегда ограничен диапазоном 1–120 дней.
    Текущий settings.json никогда не удаляется.
    """
    if STORAGE_INIT_ERROR is not None:
        return (0, 0)

    retention_days = _normalize_log_retention_days(retention_days)
    cutoff = time.time() - retention_days * 24 * 60 * 60
    removed_logs = 0
    removed_settings = 0

    # 1) Удаляем диагностические файлы старше выбранного срока.
    try:
        for path in LOG_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff and _safe_unlink(path):
                    removed_logs += 1
            except OSError:
                continue
    except OSError:
        pass

    # 2) Резервные копии повреждённых настроек: тот же срок + не более 5 штук.
    try:
        broken_files = []
        for path in SETTINGS_DIR.glob("settings.broken_*.json"):
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    if _safe_unlink(path):
                        removed_settings += 1
                    continue
                broken_files.append((stat.st_mtime, path))
            except OSError:
                continue

        broken_files.sort(key=lambda item: item[0], reverse=True)
        for _, path in broken_files[MAX_BROKEN_SETTINGS_BACKUPS:]:
            if _safe_unlink(path):
                removed_settings += 1
    except OSError:
        pass

    # 3) Жёсткий лимит всей папки логов — 1 МБ.
    try:
        files: list[tuple[float, int, Path]] = []
        total_size = 0

        for path in LOG_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            size = max(0, int(stat.st_size))
            total_size += size
            files.append((stat.st_mtime, size, path))

        if total_size > MAX_LOG_DIR_BYTES:
            files.sort(key=lambda item: item[0])
            for _, size, path in files:
                if total_size <= MAX_LOG_DIR_BYTES:
                    break
                if _safe_unlink(path):
                    total_size -= size
                    removed_logs += 1

    except OSError:
        pass

    return (removed_logs, removed_settings)


_prepare_storage()

LOGGER = logging.getLogger(APP_FOLDER)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())

LOGGING_ENABLED, ACTIVE_LOG_RETENTION_DAYS = _read_bootstrap_logging_preferences()


def _configure_logger(enabled: bool) -> None:
    """Немедленно включает или выключает запись логов без перезапуска."""
    global LOGGING_ENABLED

    LOGGING_ENABLED = bool(enabled)

    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    if not LOGGING_ENABLED or STORAGE_INIT_ERROR is not None:
        LOGGER.addHandler(logging.NullHandler())
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "computer_locker.log",
        maxBytes=MAX_LOG_FILE_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOGGER.addHandler(handler)


def _apply_logging_preferences(enabled: bool, retention_days: int) -> tuple[int, int]:
    """
    Применяет настройки диагностики сразу.

    Даже если запись выключена, старые файлы всё равно очищаются по выбранному
    сроку и общему лимиту папки.
    """
    global ACTIVE_LOG_RETENTION_DAYS

    ACTIVE_LOG_RETENTION_DAYS = _normalize_log_retention_days(retention_days)

    # Сначала закрываем активный файловый handler. На Windows открытый .log
    # нельзя надёжно удалить при очистке по лимиту размера.
    _configure_logger(False)
    cleanup_result = _cleanup_old_diagnostics(ACTIVE_LOG_RETENTION_DAYS)
    _configure_logger(enabled)
    return cleanup_result


CLEANUP_RESULT = _apply_logging_preferences(
    LOGGING_ENABLED,
    ACTIVE_LOG_RETENTION_DAYS,
)

if LOGGING_ENABLED and any(CLEANUP_RESULT):
    LOGGER.info(
        "Diagnostics cleanup | removed_logs=%s | removed_settings_backups=%s | "
        "retention_days=%s | max_log_dir_bytes=%s",
        CLEANUP_RESULT[0],
        CLEANUP_RESULT[1],
        ACTIVE_LOG_RETENTION_DAYS,
        MAX_LOG_DIR_BYTES,
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")

    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _backup_broken_settings() -> Path | None:
    if not SETTINGS_PATH.exists():
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = SETTINGS_PATH.with_name(f"settings.broken_{stamp}.json")

    try:
        os.replace(SETTINGS_PATH, backup)

        # После создания нового бэкапа сразу применяем те же ограничения.
        # Так папка настроек не раздуется даже при повторяющейся порче файла.
        _cleanup_old_diagnostics(ACTIVE_LOG_RETENTION_DAYS)
        return backup
    except OSError:
        LOGGER.exception("Failed to preserve broken settings file.")
        return None


DIAGNOSTIC_CONTEXT_PATH = LOG_DIR / "diagnostic_context.json"
LAST_PROBLEM_PATH = LOG_DIR / "last_problem.json"


def _safe_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(v) for v in value]
    return str(value)


def _write_diagnostic_context(
    event: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Маленький перезаписываемый файл-контекст для ChatGPT/Codex.

    Он не копит историю, поэтому почти не влияет на размер папки.
    """
    if not LOGGING_ENABLED or STORAGE_INIT_ERROR is not None:
        return

    payload = {
        "schema": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "event": event,
        "windows": str(sys.getwindowsversion()),
        "python": sys.version.split()[0],
        "frozen_exe": bool(getattr(sys, "frozen", False)),
        "logging": {
            "enabled": LOGGING_ENABLED,
            "retention_days": ACTIVE_LOG_RETENTION_DAYS,
            "max_log_file_bytes": MAX_LOG_FILE_BYTES,
            "max_log_dir_bytes": MAX_LOG_DIR_BYTES,
        "max_log_retention_days": MAX_LOG_RETENTION_DAYS,
        },
        "details": _safe_json_value(details or {}),
    }

    try:
        _atomic_write_json(DIAGNOSTIC_CONTEXT_PATH, payload)
    except Exception:
        LOGGER.exception("Failed to write diagnostic_context.json.")


def _record_problem(
    source: str,
    exc: BaseException | None = None,
    *,
    details: dict[str, Any] | None = None,
    exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
) -> None:
    """
    Сохраняет только ПОСЛЕДНЮЮ значимую проблему в компактном JSON.

    Полный стек ограничен по длине, поэтому даже повторяющиеся ошибки не
    раздувают папку. История при этом остаётся в ротируемом .log.
    """
    if not LOGGING_ENABLED or STORAGE_INIT_ERROR is not None:
        return

    trace_text = ""
    error_type = ""
    error_message = ""

    if exc_info is not None:
        error_type = exc_info[0].__name__
        error_message = str(exc_info[1])
        trace_text = "".join(traceback.format_exception(*exc_info))
    elif exc is not None:
        error_type = type(exc).__name__
        error_message = str(exc)
        trace_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

    if len(trace_text) > MAX_PROBLEM_TRACE_CHARS:
        trace_text = trace_text[-MAX_PROBLEM_TRACE_CHARS:]

    payload = {
        "schema": 1,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "source": source,
        "error_type": error_type,
        "message": error_message,
        "details": _safe_json_value(details or {}),
        "traceback": trace_text,
        "hint": (
            "Для анализа передайте нейросети этот файл, diagnostic_context.json "
            "и computer_locker.log."
        ),
    }

    try:
        _atomic_write_json(LAST_PROBLEM_PATH, payload)
    except Exception:
        LOGGER.exception("Failed to write last_problem.json.")



# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------


class SingleInstance:
    """
    Держит стабильный mutex и legacy mutex версии 2.0.

    Это не даёт запустить две копии новой версии и одновременно помогает
    обнаружить уже запущенную предыдущую версию 2.0.
    """

    MUTEX_NAMES = (
        r"Local\ComputerLockerSafe",
        r"Local\ComputerLockerSafe_2_0",
    )

    def __init__(self) -> None:
        self.handles: list[int] = []
        self.already_exists = False

        try:
            for name in self.MUTEX_NAMES:
                ctypes.set_last_error(0)
                handle = kernel32.CreateMutexW(None, False, name)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())

                self.handles.append(handle)
                if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                    self.already_exists = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        while self.handles:
            handle = self.handles.pop()
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Periodic keep-awake worker
# ---------------------------------------------------------------------------


class KeepAwakeWorker:
    """
    Периодически сбрасывает таймеры простоя Windows.

    В отличие от постоянного ES_CONTINUOUS здесь нет "липкого" режима:
    когда поток остановлен, новые сигналы активности больше не отправляются.
    Для фактического срока используется time.monotonic(), поэтому ручная
    смена системных часов не продлит и не сократит защитный таймер.
    """

    def __init__(self, events: queue.Queue[tuple[str, str]]) -> None:
        self.events = events
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.deadline_monotonic: float | None = None
        self.deadline_epoch: float | None = None
        self.flags = 0
        self._state_lock = threading.Lock()

    @property
    def active(self) -> bool:
        thread = self.thread
        return bool(thread and thread.is_alive())

    def remaining_seconds(self) -> int | None:
        with self._state_lock:
            deadline = self.deadline_monotonic

        if deadline is None:
            return None

        return max(0, int(deadline - time.monotonic()))

    def start(
        self,
        *,
        keep_system_awake: bool,
        keep_display_awake: bool,
        hours: int,
    ) -> None:
        self.stop()

        flags = 0
        if keep_system_awake:
            flags |= ES_SYSTEM_REQUIRED
        if keep_display_awake:
            flags |= ES_DISPLAY_REQUIRED

        if flags == 0:
            return

        duration_sec = hours * 60 * 60

        with self._state_lock:
            self.flags = flags
            self.deadline_monotonic = time.monotonic() + duration_sec
            self.deadline_epoch = time.time() + duration_sec

        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="KeepAwake",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        thread = self.thread
        self.stop_event.set()

        if (
            thread
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

        self.thread = None
        with self._state_lock:
            self.flags = 0
            self.deadline_monotonic = None
            self.deadline_epoch = None

    def _signal_activity(self) -> None:
        last_error = 0

        for attempt in range(1, KEEP_ALIVE_RETRY_COUNT + 1):
            with self._state_lock:
                flags = self.flags

            ctypes.set_last_error(0)
            result = kernel32.SetThreadExecutionState(flags)
            if result != 0:
                if attempt > 1:
                    LOGGER.warning(
                        "SetThreadExecutionState recovered | attempt=%s",
                        attempt,
                    )
                return

            last_error = ctypes.get_last_error()
            LOGGER.warning(
                "SetThreadExecutionState failed | attempt=%s/%s | error=%s",
                attempt,
                KEEP_ALIVE_RETRY_COUNT,
                last_error,
            )

            if attempt < KEEP_ALIVE_RETRY_COUNT:
                if self.stop_event.wait(KEEP_ALIVE_RETRY_DELAY_SEC):
                    return

        raise OSError(
            last_error,
            f"SetThreadExecutionState failed after {KEEP_ALIVE_RETRY_COUNT} attempts",
        )

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                remaining = self.remaining_seconds()
                if remaining is None:
                    return

                if remaining <= 0:
                    self.events.put(("expired", "Защитный таймер истёк."))
                    return

                self._signal_activity()

                remaining = self.remaining_seconds()
                if remaining is None:
                    return

                wait_seconds = min(
                    KEEP_ALIVE_INTERVAL_SEC,
                    max(0.2, float(remaining)),
                )
                if self.stop_event.wait(wait_seconds):
                    return

        except Exception as exc:
            LOGGER.exception("Keep-awake worker crashed.")
            _record_problem("keep_awake_worker", exc)
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# Session lock/unlock monitor
# ---------------------------------------------------------------------------


class SessionStateMonitor:
    """
    После успешной блокировки ждёт фактический lock, а затем unlock.

    Нужен только для удобной функции "автоматически разрешить сон после
    разблокировки". Если WTS недоступен, программа продолжит работать по
    защитному таймеру — сбой этого монитора не ломает блокировку.
    """

    def __init__(self, events: queue.Queue[tuple[str, str]]) -> None:
        self.events = events
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock_seen = False

    @property
    def active(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        self.stop()
        self.stop_event.clear()
        self.lock_seen = False
        self.thread = threading.Thread(
            target=self._run,
            name="SessionMonitor",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        thread = self.thread
        self.stop_event.set()

        if (
            thread
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

        self.thread = None
        self.lock_seen = False

    def _run(self) -> None:
        consecutive_errors = 0

        while not self.stop_event.is_set():
            try:
                locked = _query_current_session_locked()
                consecutive_errors = 0

                if locked is True and not self.lock_seen:
                    self.lock_seen = True
                    self.events.put(("session_locked", "Windows заблокирована."))

                elif locked is False and self.lock_seen:
                    self.events.put(("session_unlocked", "Windows разблокирована."))
                    return

            except Exception as exc:
                consecutive_errors += 1
                LOGGER.warning(
                    "Session state query failed | attempt=%s/%s | %s: %s",
                    consecutive_errors,
                    SESSION_QUERY_MAX_ERRORS,
                    type(exc).__name__,
                    exc,
                )

                if consecutive_errors >= SESSION_QUERY_MAX_ERRORS:
                    _record_problem(
                        "session_state_monitor",
                        exc,
                        details={"consecutive_errors": consecutive_errors},
                    )
                    self.events.put(
                        (
                            "session_monitor_error",
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
                    return

            if self.stop_event.wait(SESSION_POLL_INTERVAL_SEC):
                return


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


class ComputerLocker:
    DEFAULTS = {
        "keep_awake": True,
        "keep_display": False,
        "auto_stop_on_unlock": True,
        "hours": 12,
        "write_logs": True,
        "log_retention_days": DEFAULT_LOG_RETENTION_DAYS,
    }

    def __init__(self) -> None:
        settings = self._load_settings()
        self.last_valid_hours = int(settings["hours"])

        self._enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — {APP_VERSION}")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self._report_callback_exception

        self.keep_awake_var = tk.BooleanVar(value=bool(settings["keep_awake"]))
        self.keep_display_var = tk.BooleanVar(value=bool(settings["keep_display"]))
        self.auto_stop_var = tk.BooleanVar(
            value=bool(settings["auto_stop_on_unlock"])
        )
        self.hours_var = tk.StringVar(value=str(settings["hours"]))
        self.write_logs_var = tk.BooleanVar(value=bool(settings["write_logs"]))
        self.log_days_var = tk.StringVar(value=str(settings["log_retention_days"]))
        self.last_valid_log_days = int(settings["log_retention_days"])

        _apply_logging_preferences(
            self.write_logs_var.get(),
            self.last_valid_log_days,
        )

        self.worker_events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.keep_awake_worker = KeepAwakeWorker(self.worker_events)
        self.session_monitor = SessionStateMonitor(self.worker_events)

        self.poll_job: str | None = None
        self.countdown_job: str | None = None
        self.closing = False

        self._build_ui()
        self._center_window()
        self._sync_power_controls()
        self._sync_logging_controls()
        self._schedule_worker_poll()

        _write_diagnostic_context("application_start", details={"settings": settings})
        LOGGER.info(
            "Start | version=%s | windows=%s | python=%s | settings=%s | program_dir=%s | retention_days=%s",
            APP_VERSION,
            sys.getwindowsversion(),
            sys.version.split()[0],
            settings,
            PROGRAM_DIR,
            ACTIVE_LOG_RETENTION_DAYS,
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    @staticmethod
    def _enable_dpi_awareness() -> None:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            frame,
            text=APP_NAME,
            font=("Segoe UI", 14, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(
            frame,
            text=(
                "Windows будет заблокирована штатно. Фоновые программы могут\n"
                "продолжать работу. Для входа нужен обычный пароль или PIN Windows."
            ),
            justify="left",
        ).grid(
            row=1,
            column=0,
            columnspan=3,
            pady=(5, 14),
            sticky="w",
        )

        power_box = ttk.LabelFrame(
            frame,
            text="Питание во время блокировки",
            padding=12,
        )
        power_box.grid(row=2, column=0, columnspan=3, sticky="ew")

        ttk.Checkbutton(
            power_box,
            text="Не давать компьютеру автоматически уснуть",
            variable=self.keep_awake_var,
            command=self._on_keep_awake_changed,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self.display_check = ttk.Checkbutton(
            power_box,
            text="Не выключать монитор во время блокировки",
            variable=self.keep_display_var,
            command=self._on_keep_display_changed,
        )
        self.display_check.grid(
            row=1,
            column=0,
            columnspan=3,
            pady=(6, 0),
            sticky="w",
        )

        ttk.Label(
            power_box,
            text=(
                "Фоновым программам включённый монитор не нужен. "
                "Эта опция по умолчанию выключена.\n"
                "Включайте её только если экран после блокировки плохо просыпается."
            ),
            foreground="#555555",
            justify="left",
        ).grid(
            row=2,
            column=0,
            columnspan=3,
            padx=(22, 0),
            pady=(4, 8),
            sticky="w",
        )

        self.auto_stop_check = ttk.Checkbutton(
            power_box,
            text="Автоматически разрешить сон после разблокировки",
            variable=self.auto_stop_var,
        )
        self.auto_stop_check.grid(
            row=3,
            column=0,
            columnspan=3,
            pady=(4, 0),
            sticky="w",
        )

        ttk.Label(
            power_box,
            text=(
                "Если вы вернулись раньше защитного таймера, программа сама "
                "прекратит поддержание активности."
            ),
            foreground="#555555",
        ).grid(
            row=4,
            column=0,
            columnspan=3,
            padx=(22, 0),
            pady=(4, 8),
            sticky="w",
        )

        ttk.Label(power_box, text="Защитный таймер:").grid(
            row=5,
            column=0,
            pady=(8, 0),
            sticky="w",
        )

        self.hours_spinbox = ttk.Spinbox(
            power_box,
            from_=1,
            to=72,
            width=7,
            textvariable=self.hours_var,
        )
        self.hours_spinbox.grid(
            row=5,
            column=1,
            padx=(10, 4),
            pady=(8, 0),
            sticky="w",
        )

        ttk.Label(power_box, text="часов (1–72)").grid(
            row=5,
            column=2,
            pady=(8, 0),
            sticky="w",
        )

        ttk.Label(
            power_box,
            text=(
                "После истечения времени программа перестанет сбрасывать "
                "таймер сна Windows."
            ),
            foreground="#555555",
        ).grid(
            row=6,
            column=0,
            columnspan=3,
            pady=(5, 0),
            sticky="w",
        )

        diagnostics_box = ttk.LabelFrame(
            frame,
            text="Логи проблем",
            padding=12,
        )
        diagnostics_box.grid(
            row=3,
            column=0,
            columnspan=3,
            pady=(12, 0),
            sticky="ew",
        )

        self.write_logs_check = ttk.Checkbutton(
            diagnostics_box,
            text="Писать логи проблем",
            variable=self.write_logs_var,
            command=self._on_logging_changed,
        )
        self.write_logs_check.grid(
            row=0,
            column=0,
            columnspan=3,
            sticky="w",
        )

        ttk.Label(
            diagnostics_box,
            text="Хранить логи:",
        ).grid(row=1, column=0, pady=(8, 0), sticky="w")

        self.log_days_spinbox = ttk.Spinbox(
            diagnostics_box,
            from_=MIN_LOG_RETENTION_DAYS,
            to=MAX_LOG_RETENTION_DAYS,
            width=7,
            textvariable=self.log_days_var,
        )
        self.log_days_spinbox.grid(
            row=1,
            column=1,
            padx=(10, 4),
            pady=(8, 0),
            sticky="w",
        )

        ttk.Label(
            diagnostics_box,
            text=f"дней ({MIN_LOG_RETENTION_DAYS}–{MAX_LOG_RETENTION_DAYS})",
        ).grid(row=1, column=2, pady=(8, 0), sticky="w")

        ttk.Label(
            diagnostics_box,
            text=(
                "Для нейросети сохраняется компактный контекст и последняя проблема. "
                "Один .log ≤ 256 КБ, вся папка ≤ 1 МБ."
            ),
            foreground="#555555",
            wraplength=560,
            justify="left",
        ).grid(
            row=2,
            column=0,
            columnspan=3,
            pady=(5, 0),
            sticky="w",
        )

        status_box = ttk.LabelFrame(frame, text="Состояние", padding=12)
        status_box.grid(
            row=4,
            column=0,
            columnspan=3,
            pady=(12, 0),
            sticky="ew",
        )

        self.status_label = ttk.Label(
            status_box,
            text="Готово к блокировке.",
            justify="left",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        self.countdown_label = ttk.Label(
            status_box,
            text="Поддержание активности: выключено",
        )
        self.countdown_label.grid(row=1, column=0, pady=(4, 0), sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(
            row=5,
            column=0,
            columnspan=3,
            pady=(16, 0),
            sticky="ew",
        )

        self.lock_button = ttk.Button(
            buttons,
            text="Заблокировать компьютер",
            command=self.lock_computer,
        )
        self.lock_button.pack(side="left")

        self.stop_button = ttk.Button(
            buttons,
            text="Разрешить сон",
            command=self.stop_keep_awake,
        )
        self.stop_button.pack(side="left", padx=(10, 0))

        self.logs_button = ttk.Button(
            buttons,
            text="Логи проблем",
            command=self.open_logs,
        )
        self.logs_button.pack(side="left", padx=(10, 0))

        ttk.Button(
            buttons,
            text="Выход",
            command=self.close,
        ).pack(side="right", padx=(16, 0))

        ttk.Label(
            frame,
            text=f"Версия {APP_VERSION} · настройки и логи хранятся рядом с программой",
            foreground="#777777",
        ).grid(
            row=6,
            column=0,
            columnspan=3,
            pady=(12, 0),
            sticky="w",
        )

        self._refresh_action_buttons()

    def _center_window(self) -> None:
        self.root.update_idletasks()

        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)

        self.root.geometry(f"+{x}+{y}")

    def _on_keep_awake_changed(self) -> None:
        if not self.keep_awake_var.get() and self.keep_display_var.get():
            self.keep_display_var.set(False)
        self._sync_power_controls()

    def _on_keep_display_changed(self) -> None:
        if self.keep_display_var.get():
            self.keep_awake_var.set(True)
        self._sync_power_controls()

    def _sync_power_controls(self) -> None:
        enabled = self.keep_awake_var.get()
        self.hours_spinbox.configure(state="normal" if enabled else "disabled")
        self.auto_stop_check.configure(state="normal" if enabled else "disabled")
        self._refresh_action_buttons()

    def _sync_logging_controls(self) -> None:
        state = "normal" if self.write_logs_var.get() else "disabled"
        self.log_days_spinbox.configure(state=state)

    def _on_logging_changed(self) -> None:
        self._sync_logging_controls()
        days = self._get_log_retention_days(require_valid=False)
        _apply_logging_preferences(self.write_logs_var.get(), days)
        self._save_settings()

        if self.write_logs_var.get():
            _write_diagnostic_context(
                "logging_enabled",
                details={"retention_days": days},
            )
            self.status_label.configure(
                text=f"Логи проблем включены. Хранение: {days} дней."
            )
        else:
            self.status_label.configure(
                text="Запись логов проблем выключена."
            )

    def _get_log_retention_days(self, *, require_valid: bool = True) -> int:
        try:
            days = int(self.log_days_var.get().strip())
        except ValueError as exc:
            if require_valid:
                raise ValueError(
                    f"Укажите срок хранения логов от {MIN_LOG_RETENTION_DAYS} "
                    f"до {MAX_LOG_RETENTION_DAYS} дней."
                ) from exc
            return self.last_valid_log_days

        if not MIN_LOG_RETENTION_DAYS <= days <= MAX_LOG_RETENTION_DAYS:
            if require_valid:
                raise ValueError(
                    f"Укажите срок хранения логов от {MIN_LOG_RETENTION_DAYS} "
                    f"до {MAX_LOG_RETENTION_DAYS} дней."
                )
            return self.last_valid_log_days

        self.last_valid_log_days = days
        return days

    def _apply_logging_from_ui(self, *, require_valid: bool = True) -> int:
        days = self._get_log_retention_days(require_valid=require_valid)
        _apply_logging_preferences(self.write_logs_var.get(), days)
        return days

    def _refresh_action_buttons(self) -> None:
        if not hasattr(self, "stop_button"):
            return
        self.stop_button.configure(
            state="normal" if self.keep_awake_worker.active else "disabled"
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _load_settings(self) -> dict[str, Any]:
        settings = dict(self.DEFAULTS)

        try:
            if SETTINGS_PATH.exists():
                with SETTINGS_PATH.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)

                if not isinstance(loaded, dict):
                    raise ValueError("Корень settings.json должен быть JSON-объектом.")

                settings.update(loaded)

        except Exception:
            LOGGER.exception("Failed to load settings.")
            backup = _backup_broken_settings()
            if backup is not None:
                LOGGER.warning("Broken settings moved to %s", backup)

        settings["keep_awake"] = bool(settings.get("keep_awake", True))
        settings["keep_display"] = bool(settings.get("keep_display", False))
        settings["auto_stop_on_unlock"] = bool(
            settings.get("auto_stop_on_unlock", True)
        )
        settings["write_logs"] = bool(settings.get("write_logs", True))
        settings["log_retention_days"] = _normalize_log_retention_days(
            settings.get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS)
        )

        try:
            hours = int(settings.get("hours", 12))
        except (TypeError, ValueError):
            hours = 12

        settings["hours"] = min(72, max(1, hours))

        if settings["keep_display"]:
            settings["keep_awake"] = True

        return settings

    def _save_settings(self) -> None:
        hours = self._get_hours(require_valid=False)

        log_days = self._get_log_retention_days(require_valid=False)

        data = {
            "keep_awake": self.keep_awake_var.get(),
            "keep_display": self.keep_display_var.get(),
            "auto_stop_on_unlock": self.auto_stop_var.get(),
            "hours": hours,
            "write_logs": self.write_logs_var.get(),
            "log_retention_days": log_days,
        }

        try:
            _atomic_write_json(SETTINGS_PATH, data)
        except Exception:
            LOGGER.exception("Failed to save settings.")

    def _get_hours(self, *, require_valid: bool = True) -> int:
        try:
            hours = int(self.hours_var.get().strip())
        except ValueError as exc:
            if require_valid:
                raise ValueError("Введите целое количество часов.") from exc
            return self.last_valid_hours

        if not 1 <= hours <= 72:
            if require_valid:
                raise ValueError("Укажите время от 1 до 72 часов.")
            return self.last_valid_hours

        self.last_valid_hours = hours
        return hours

    # ------------------------------------------------------------------
    # Keep-awake / session events / countdown
    # ------------------------------------------------------------------

    def _schedule_worker_poll(self) -> None:
        if self.closing:
            return

        self.poll_job = self.root.after(250, self._poll_worker_events)

    def _poll_worker_events(self) -> None:
        self.poll_job = None

        while True:
            try:
                event, details = self.worker_events.get_nowait()
            except queue.Empty:
                break

            if event == "expired":
                LOGGER.info("Keep-awake timer expired.")
                _write_diagnostic_context("keep_awake_timer_expired")
                self.keep_awake_worker.stop()
                self.session_monitor.stop()
                self._cancel_countdown()
                self.status_label.configure(
                    text=(
                        "Защитный таймер истёк. "
                        "Сон снова контролирует Windows."
                    )
                )
                self.countdown_label.configure(
                    text="Поддержание активности: выключено"
                )
                self._refresh_action_buttons()

            elif event == "error":
                LOGGER.error("Keep-awake worker error: %s", details)
                self.keep_awake_worker.stop()
                self.session_monitor.stop()
                self._cancel_countdown()
                self.status_label.configure(
                    text="Не удалось поддерживать активность Windows."
                )
                self.countdown_label.configure(
                    text="Поддержание активности: ошибка"
                )
                self._refresh_action_buttons()
                messagebox.showerror(
                    "Ошибка управления питанием",
                    f"{details}\n\nПодробности записаны в:\n{LOG_DIR}",
                    parent=self.root,
                )

            elif event == "session_locked":
                LOGGER.info("Session monitor confirmed Windows lock.")
                _write_diagnostic_context("session_locked")
                if self.keep_awake_worker.active:
                    self.status_label.configure(
                        text="Windows заблокирована. Поддержание активности работает."
                    )

            elif event == "session_unlocked":
                LOGGER.info("Session monitor confirmed Windows unlock.")
                _write_diagnostic_context("session_unlocked")
                if self.auto_stop_var.get() and self.keep_awake_worker.active:
                    self.keep_awake_worker.stop()
                    self.session_monitor.stop()
                    self._cancel_countdown()
                    self.countdown_label.configure(
                        text="Поддержание активности: выключено"
                    )
                    self.status_label.configure(
                        text=(
                            "Windows разблокирована. Поддержание активности "
                            "остановлено автоматически."
                        )
                    )
                    self._refresh_action_buttons()

            elif event == "session_monitor_error":
                LOGGER.warning("Session monitor disabled: %s", details)
                self.session_monitor.stop()
                if self.keep_awake_worker.active and self.auto_stop_var.get():
                    self.status_label.configure(
                        text=(
                            "Автоотключение после разблокировки недоступно. "
                            "Защитный таймер продолжает работать."
                        )
                    )

        self._schedule_worker_poll()

    def _start_countdown(self) -> None:
        self._cancel_countdown()
        self._countdown_tick()

    def _countdown_tick(self) -> None:
        self.countdown_job = None

        if not self.keep_awake_worker.active:
            self.countdown_label.configure(
                text="Поддержание активности: выключено"
            )
            self._refresh_action_buttons()
            return

        remaining = self.keep_awake_worker.remaining_seconds()
        if remaining is None:
            self.countdown_label.configure(
                text="Поддержание активности: выключено"
            )
            self._refresh_action_buttons()
            return

        hours, remainder = divmod(remaining, 3600)
        minutes, seconds = divmod(remainder, 60)

        display_note = " + монитор" if self.keep_display_var.get() else ""
        self.countdown_label.configure(
            text=(
                "До отключения поддержания: "
                f"{hours:02d}:{minutes:02d}:{seconds:02d}{display_note}"
            )
        )
        self._refresh_action_buttons()

        self.countdown_job = self.root.after(1000, self._countdown_tick)

    def _cancel_countdown(self) -> None:
        if self.countdown_job is not None:
            try:
                self.root.after_cancel(self.countdown_job)
            except tk.TclError:
                pass
            self.countdown_job = None

    def stop_keep_awake(self) -> None:
        was_active = self.keep_awake_worker.active
        self.keep_awake_worker.stop()
        self.session_monitor.stop()
        self._cancel_countdown()

        self.countdown_label.configure(
            text="Поддержание активности: выключено"
        )
        self._refresh_action_buttons()

        if was_active:
            self.status_label.configure(
                text=(
                    "Поддержание активности остановлено. "
                    "Дальше действуют настройки питания Windows."
                )
            )
            LOGGER.info("Keep-awake stopped manually.")
            _write_diagnostic_context("keep_awake_stopped_manually")

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------

    def lock_computer(self) -> None:
        self.lock_button.configure(state="disabled")

        try:
            keep_awake = self.keep_awake_var.get()
            keep_display = self.keep_display_var.get()
            auto_stop = self.auto_stop_var.get()
            hours = self._get_hours(require_valid=keep_awake)
            log_days = self._apply_logging_from_ui(
                require_valid=self.write_logs_var.get()
            )

            self._save_settings()
            _write_diagnostic_context(
                "lock_requested",
                details={
                    "keep_awake": keep_awake,
                    "keep_display": keep_display,
                    "auto_stop_on_unlock": auto_stop,
                    "hours": hours,
                    "log_retention_days": log_days,
                },
            )

            if keep_awake:
                self.keep_awake_worker.start(
                    keep_system_awake=True,
                    keep_display_awake=keep_display,
                    hours=hours,
                )
                self._start_countdown()

                modes = "сон компьютера"
                if keep_display:
                    modes += " и выключение монитора"

                self.status_label.configure(
                    text=f"Перед блокировкой контролируется: {modes}."
                )
            else:
                self.stop_keep_awake()
                self.status_label.configure(
                    text="Блокировка без изменения параметров питания Windows."
                )

            LOGGER.info(
                "Lock requested | keep_awake=%s | keep_display=%s | "
                "auto_stop_on_unlock=%s | hours=%s",
                keep_awake,
                keep_display,
                auto_stop,
                hours,
            )

            self.root.withdraw()
            self.root.update_idletasks()

            ctypes.set_last_error(0)
            if not user32.LockWorkStation():
                raise ctypes.WinError(ctypes.get_last_error())

            # Монитор запускаем только после того, как LockWorkStation принят.
            # Он сначала должен увидеть LOCK, и только после этого реагирует на UNLOCK.
            if keep_awake and auto_stop:
                self.session_monitor.start()

            # Fallback: даже если WTS-монитор недоступен, окно не останется скрытым.
            # Пока Windows заблокирована, системный защищённый экран всё равно выше.
            self.root.after(2000, self._show_window)

        except ValueError as exc:
            self.keep_awake_worker.stop()
            self.session_monitor.stop()
            self._cancel_countdown()
            self._show_window()
            self._refresh_action_buttons()
            messagebox.showwarning(
                "Проверьте настройки",
                str(exc),
                parent=self.root,
            )

        except Exception as exc:
            self.keep_awake_worker.stop()
            self.session_monitor.stop()
            self._cancel_countdown()
            self._show_window()
            self._refresh_action_buttons()

            LOGGER.exception("LockWorkStation failed.")
            _record_problem("lock_computer", exc)
            messagebox.showerror(
                "Не удалось заблокировать компьютер",
                (
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"Подробности записаны в:\n{LOG_DIR}"
                ),
                parent=self.root,
            )

        finally:
            try:
                self.lock_button.configure(state="normal")
                self._refresh_action_buttons()
            except tk.TclError:
                pass

    def _show_window(self) -> None:
        if self.closing:
            return

        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(350, self._remove_topmost)
        except tk.TclError:
            pass

    def _remove_topmost(self) -> None:
        if self.closing:
            return
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Diagnostics / lifecycle
    # ------------------------------------------------------------------

    def open_logs(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _cleanup_old_diagnostics(
                self._get_log_retention_days(require_valid=False)
            )
            os.startfile(str(LOG_DIR))
        except Exception as exc:
            LOGGER.exception("Failed to open log folder.")
            messagebox.showerror(
                "Не удалось открыть папку логов",
                f"{type(exc).__name__}: {exc}",
                parent=self.root,
            )

    def _report_callback_exception(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
    ) -> None:
        LOGGER.error(
            "Tkinter callback error",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        _record_problem(
            "tkinter_callback",
            details={"callback_error": True},
            exc_info=(exc_type, exc_value, exc_traceback),
        )

        try:
            messagebox.showerror(
                "Ошибка программы",
                (
                    f"{exc_type.__name__}: {exc_value}\n\n"
                    f"Подробности записаны в:\n{LOG_DIR}"
                ),
                parent=self.root,
            )
        except tk.TclError:
            pass

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True

        LOGGER.info("Application closing.")
        _write_diagnostic_context("application_closing")
        self._save_settings()

        self.session_monitor.stop()
        self.keep_awake_worker.stop()
        self._cancel_countdown()

        if self.poll_job is not None:
            try:
                self.root.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None

        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def _self_test() -> int:
    """
    Безопасная проверка для будущего GitHub Actions на Windows runner.
    Ничего не блокирует и не меняет настройки питания.
    """
    session_query: str
    try:
        state = _query_current_session_locked()
        session_query = "unknown" if state is None else ("locked" if state else "unlocked")
    except Exception as exc:
        session_query = f"unavailable: {type(exc).__name__}: {exc}"

    checks = {
        "LockWorkStation": bool(user32.LockWorkStation),
        "SetThreadExecutionState": bool(kernel32.SetThreadExecutionState),
        "WTSQuerySessionInformationW": bool(wtsapi32.WTSQuerySessionInformationW),
        "session_state": session_query,
        "program_dir": str(PROGRAM_DIR),
        "settings_path": str(SETTINGS_PATH),
        "log_dir": str(LOG_DIR),
        "storage_ready": STORAGE_INIT_ERROR is None,
        "logging_enabled": LOGGING_ENABLED,
        "retention_days": ACTIVE_LOG_RETENTION_DAYS,
        "max_log_file_bytes": MAX_LOG_FILE_BYTES,
        "max_log_dir_bytes": MAX_LOG_DIR_BYTES,
        "max_log_retention_days": MAX_LOG_RETENTION_DAYS,
        "version": APP_VERSION,
    }

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print("self-test: ok")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    if STORAGE_INIT_ERROR is not None:
        try:
            user32.MessageBoxW(
                None,
                (
                    "Не удалось создать папки настроек и логов рядом с программой.\n\n"
                    f"Папка программы:\n{PROGRAM_DIR}\n\n"
                    f"Ошибка: {type(STORAGE_INIT_ERROR).__name__}: {STORAGE_INIT_ERROR}\n\n"
                    "Переместите программу в папку, где у вашей учётной записи "
                    "есть право записи, и запустите снова."
                ),
                f"{APP_NAME} — нет доступа к папке",
                0x00000010,
            )
        except Exception:
            pass
        return 1

    instance: SingleInstance | None = None

    try:
        instance = SingleInstance()

        if instance.already_exists:
            user32.MessageBoxW(
                None,
                "Программа уже запущена.",
                APP_NAME,
                0x00000040,
            )
            return 0

        ComputerLocker().run()
        return 0

    except Exception as exc:
        LOGGER.exception("Critical startup error.")
        _record_problem("critical_startup", exc)

        try:
            user32.MessageBoxW(
                None,
                (
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"Лог:\n{LOG_DIR / 'computer_locker.log'}"
                ),
                f"{APP_NAME} — ошибка",
                0x00000010,
            )
        except Exception:
            pass

        return 1

    finally:
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
