"""
RESTRUCT — Проверка на читы (GTA5RP): backend

Вся логика проверки (процессы, диск, корзина, активность, подпись, логи) —
без единой строчки UI. Интерфейс (app.py + web/) дёргает эти функции через
JS-мост pywebview. Ничего здесь никуда не отправляет по сети.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime
from pathlib import Path

import psutil

try:
    import win32process
    import win32api
    import win32con

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


# ---------------------------------------------------------------------------
# Проверка цифровой подписи файла (WinVerifyTrust) — используется для разбора
# отдельного процесса по клику. Не запускается массово для всего списка сразу,
# т.к. на процесс уходит заметное время (чтение + проверка файла).
# ---------------------------------------------------------------------------

_WINTRUST_ACTION_GENERIC_VERIFY_V2 = "{00AAC56B-CD44-11d0-8CC2-00C04FC295EE}"
_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_SAFER_FLAG = 0x100


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


def _guid_from_string(guid_str: str) -> _GUID:
    u = uuid.UUID(guid_str)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    g.Data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return g


def check_signature(path: str) -> str:
    """Возвращает 'signed' / 'unsigned' / 'unknown'. Использует нативный WinVerifyTrust —
    ту же проверку Authenticode, что и сам Windows ("Свойства файла → Цифровые подписи")."""
    if not path or not os.path.isfile(path):
        return "unknown"
    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
        wintrust.WinVerifyTrust.restype = ctypes.c_long
        wintrust.WinVerifyTrust.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_WINTRUST_DATA),
        ]

        file_info = _WINTRUST_FILE_INFO()
        file_info.cbStruct = ctypes.sizeof(_WINTRUST_FILE_INFO)
        file_info.pcwszFilePath = path
        file_info.hFile = None
        file_info.pgKnownSubject = None

        data = _WINTRUST_DATA()
        ctypes.memset(ctypes.byref(data), 0, ctypes.sizeof(data))
        data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
        data.dwUIChoice = _WTD_UI_NONE
        data.fdwRevocationChecks = _WTD_REVOKE_NONE
        data.dwUnionChoice = _WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = _WTD_STATEACTION_VERIFY
        data.dwProvFlags = _WTD_SAFER_FLAG

        guid = _guid_from_string(_WINTRUST_ACTION_GENERIC_VERIFY_V2)
        result = wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))

        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))

        return "signed" if result == 0 else "unsigned"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Локальные логи — каждый запуск проверки/скана сохраняется файлом рядом с exe,
# чтобы результат оставался даже после закрытия окна. Никуда не отправляется.
# ---------------------------------------------------------------------------

def _log_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    d = base / "logs"
    d.mkdir(exist_ok=True)
    return d


def write_log(prefix: str, text: str) -> Path | None:
    path = _log_dir() / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def list_logs() -> list[Path]:
    try:
        return sorted(_log_dir().glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Корзина — недавно удалённые файлы. Частый способ спрятать чит перед проверкой:
# удалить его за минуту до созвона. Разбираем служебные $I-файлы Windows
# (Vista+/10 формат) напрямую — никаких сторонних библиотек не требуется.
# ---------------------------------------------------------------------------

_FILETIME_EPOCH_DELTA = 116444736000000000  # 100-нс интервалов между 1601 и 1970


def _filetime_to_datetime(filetime: int) -> datetime | None:
    if filetime <= 0:
        return None
    try:
        return datetime.fromtimestamp((filetime - _FILETIME_EPOCH_DELTA) / 10_000_000)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class RecycleBinItem:
    original_path: str
    deleted_at: datetime | None
    size: int
    flagged: bool


def _parse_recycle_index_file(index_path: Path) -> tuple[str, datetime | None, int] | None:
    try:
        data = index_path.read_bytes()
    except OSError:
        return None
    if len(data) < 24:
        return None

    version = int.from_bytes(data[0:8], "little", signed=True)
    file_size = int.from_bytes(data[8:16], "little", signed=True)
    filetime_raw = int.from_bytes(data[16:24], "little", signed=False)
    deleted_at = _filetime_to_datetime(filetime_raw)

    try:
        if version == 2:
            name_len = int.from_bytes(data[24:28], "little", signed=True)
            raw = data[28 : 28 + max(name_len, 0) * 2]
            original_path = raw.decode("utf-16-le", errors="ignore")
        else:
            raw = data[24:24 + 520]
            original_path = raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
    except Exception:
        return None

    return original_path, deleted_at, max(file_size, 0)


def scan_recycle_bin(config: dict) -> list[RecycleBinItem]:
    flagged_names = {n.lower() for n in config.get("flagged_file_names", [])}
    flagged_keywords = [k.lower() for k in config.get("flagged_file_keywords", []) if k.strip()]
    items: list[RecycleBinItem] = []

    for root in get_scan_roots():
        bin_dir = root / "$Recycle.Bin"
        if not bin_dir.exists():
            continue
        try:
            sid_dirs = list(bin_dir.iterdir())
        except OSError:
            continue
        for sid_dir in sid_dirs:
            try:
                entries = list(sid_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.name.startswith("$I"):
                    continue
                parsed = _parse_recycle_index_file(entry)
                if parsed is None:
                    continue
                original_path, deleted_at, size = parsed
                name = Path(original_path).name if original_path else entry.name
                lname = name.lower()
                flagged = lname in flagged_names or any(kw in lname for kw in flagged_keywords)
                items.append(
                    RecycleBinItem(original_path=original_path or name, deleted_at=deleted_at, size=size, flagged=flagged)
                )
    items.sort(key=lambda i: i.deleted_at or datetime.min, reverse=True)
    return items


def _config_path() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "flagged_processes.json"


DEFAULT_CONFIG = {
    "flagged_process_names": [
        "cheatengine-x86_64.exe",
        "cheatengine-i386.exe",
        "x64dbg.exe",
        "x32dbg.exe",
        "extremeinjector.exe",
        "injector.exe",
        "dllinjector.exe",
        "processhacker.exe",
        "artmoney.exe",
    ],
    "game_process_names": [
        "gta5.exe",
        "gtavrp.exe",
        "ragemp_v.exe",
        "ragemp.exe",
        "fivem.exe",
        "fivem_gta5.exe",
    ],
    "suspicious_module_dir_markers": [
        "\\temp\\",
        "\\appdata\\local\\temp\\",
        "\\downloads\\",
        "\\desktop\\",
        "\\appdata\\roaming\\",
    ],
    "flagged_file_names": [
        "cheatengine-x86_64.exe",
        "cheatengine-i386.exe",
        "x64dbg.exe",
        "x32dbg.exe",
        "extremeinjector.exe",
        "dllinjector.exe",
        "artmoney.exe",
    ],
    "flagged_file_keywords": [],
    "skip_dir_names": [
        "$recycle.bin",
        "system volume information",
        "windows.old",
    ],
}


def load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, default_value in DEFAULT_CONFIG.items():
            data.setdefault(key, default_value)
        return data
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Логика сканирования (без UI) — переиспользуется обеими вкладками
# ---------------------------------------------------------------------------

@dataclass
class ProcInfo:
    pid: int
    name: str
    exe: str
    flagged: bool


@dataclass
class ModuleFinding:
    name: str
    path: str
    flagged: bool


@dataclass
class ScanResult:
    all_processes: list[ProcInfo] = field(default_factory=list)
    flagged_processes: list[ProcInfo] = field(default_factory=list)
    game_process_found: bool = False
    game_process_name: str = ""
    module_findings: list[ModuleFinding] = field(default_factory=list)
    module_scan_error: str = ""

    @property
    def flagged_modules(self) -> list[ModuleFinding]:
        return [m for m in self.module_findings if m.flagged]

    @property
    def total_flags(self) -> int:
        return len(self.flagged_processes) + len(self.flagged_modules)


def list_all_processes(config: dict) -> list[ProcInfo]:
    flagged_names = {n.lower() for n in config["flagged_process_names"]}
    result: list[ProcInfo] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").strip()
            exe = proc.info.get("exe") or "—"
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not name:
            continue
        result.append(ProcInfo(pid=pid, name=name, exe=exe, flagged=name.lower() in flagged_names))
    result.sort(key=lambda p: p.name.lower())
    return result


def _find_game_process(config: dict) -> psutil.Process | None:
    targets = {n.lower() for n in config["game_process_names"]}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in targets:
            return proc
    return None


def scan_game_modules(config: dict) -> tuple[bool, str, list[ModuleFinding], str]:
    proc = _find_game_process(config)
    if proc is None:
        return False, "", [], ""
    if not HAS_WIN32:
        return True, proc.name(), [], "pywin32 недоступен — список модулей получить нельзя."

    markers = [m.lower() for m in config["suspicious_module_dir_markers"]]
    findings: list[ModuleFinding] = []
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, proc.pid
        )
    except Exception:
        return True, proc.name(), [], "Нет доступа к процессу — запустите чекер от имени администратора."

    try:
        modules = win32process.EnumProcessModulesEx(handle, win32process.LIST_MODULES_ALL)
        for module in modules:
            try:
                path = win32process.GetModuleFileNameEx(handle, module)
            except Exception:
                continue
            lower_path = path.lower()
            is_suspicious = any(marker in lower_path for marker in markers)
            findings.append(ModuleFinding(name=Path(path).name, path=path, flagged=is_suspicious))
    finally:
        win32api.CloseHandle(handle)

    findings.sort(key=lambda f: (not f.flagged, f.name.lower()))
    return True, proc.name(), findings, ""


def run_full_scan(config: dict) -> ScanResult:
    all_processes = list_all_processes(config)
    flagged_processes = [p for p in all_processes if p.flagged]
    game_found, game_name, module_findings, module_error = scan_game_modules(config)
    return ScanResult(
        all_processes=all_processes,
        flagged_processes=flagged_processes,
        game_process_found=game_found,
        game_process_name=game_name,
        module_findings=module_findings,
        module_scan_error=module_error,
    )


# ---------------------------------------------------------------------------
# Полное сканирование диска — обходит все локальные разделы файл за файлом
# (включая System32 и другие системные папки), сверяя имена файлов со списком.
# Ничего не читает по содержимому и никуда не отправляет — только имена/пути.
# ---------------------------------------------------------------------------

@dataclass
class FileFinding:
    path: str
    matched_rule: str


def get_scan_roots() -> list[Path]:
    roots: list[Path] = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts.lower():
            continue
        if not part.fstype:
            continue
        roots.append(Path(part.mountpoint))
    return roots


def scan_full_disk(
    config: dict,
    stop_event: threading.Event,
    result_queue: "queue.Queue[tuple[str, object]]",
) -> None:
    """Работает в фоновом потоке. Кладёт в result_queue сообщения трёх типов:
    ("progress", scanned_count), ("match", FileFinding), ("done", (scanned, matches))."""
    flagged_names = {n.lower() for n in config.get("flagged_file_names", [])}
    flagged_keywords = [k.lower() for k in config.get("flagged_file_keywords", []) if k.strip()]
    skip_dirs = {d.lower() for d in config.get("skip_dir_names", [])}

    scanned = 0
    matches: list[FileFinding] = []
    last_progress_sent = 0

    for root in get_scan_roots():
        for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, onerror=lambda e: None):
            if stop_event.is_set():
                result_queue.put(("done", (scanned, matches)))
                return
            dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]

            for fname in filenames:
                if stop_event.is_set():
                    result_queue.put(("done", (scanned, matches)))
                    return
                scanned += 1
                lname = fname.lower()

                rule: str | None = None
                if lname in flagged_names:
                    rule = f"имя файла «{fname}»"
                else:
                    for kw in flagged_keywords:
                        if kw in lname:
                            rule = f"ключевое слово «{kw}»"
                            break

                if rule:
                    finding = FileFinding(path=str(Path(dirpath) / fname), matched_rule=rule)
                    matches.append(finding)
                    result_queue.put(("match", finding))

                if scanned - last_progress_sent >= 250:
                    last_progress_sent = scanned
                    result_queue.put(("progress", scanned))

    result_queue.put(("done", (scanned, matches)))


# ---------------------------------------------------------------------------
# Недавняя активность — свой аналог LastActivityView/Shellbag Analyzer без
# сторонних бинарников: Prefetch (какие программы недавно запускались) и
# MRU-ключи реестра (какие пути/команды недавно вводились). Читает только
# метаданные, ничего не отправляет.
# ---------------------------------------------------------------------------

try:
    import winreg

    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False


@dataclass
class PrefetchEntry:
    exe_name: str
    last_run: datetime | None
    flagged: bool


def list_prefetch_activity(config: dict) -> tuple[list[PrefetchEntry], str]:
    """Windows пишет .pf-файл при каждом запуске программы — по имени файла и
    времени изменения можно увидеть, что недавно запускалось, даже если саму
    программу уже удалили. Папка обычно требует прав администратора."""
    flagged_names = {n.lower() for n in config.get("flagged_process_names", [])} | {
        n.lower() for n in config.get("flagged_file_names", [])
    }
    flagged_keywords = [k.lower() for k in config.get("flagged_file_keywords", []) if k.strip()]

    prefetch_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Prefetch"
    entries: list[PrefetchEntry] = []
    if not prefetch_dir.exists():
        return entries, "Папка Prefetch не найдена."
    try:
        files = list(prefetch_dir.glob("*.pf"))
    except OSError as exc:
        return entries, f"Нет доступа к Prefetch — запустите чекер от имени администратора ({exc})."

    for f in files:
        name = f.stem.rsplit("-", 1)[0]
        exe_name = name if name.lower().endswith(".exe") else f"{name}.exe"
        try:
            last_run = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            last_run = None
        lname = exe_name.lower()
        flagged = lname in flagged_names or any(kw in lname for kw in flagged_keywords)
        entries.append(PrefetchEntry(exe_name=exe_name, last_run=last_run, flagged=flagged))

    entries.sort(key=lambda e: e.last_run or datetime.min, reverse=True)
    return entries, ""


@dataclass
class MRUEntry:
    source: str
    value: str
    flagged: bool


def list_recent_mru(config: dict) -> list[MRUEntry]:
    """Урезанный аналог Shellbag Analyzer: не восстанавливает имена удалённых
    папок (это требует полноценного разбора бинарного формата shellbag), но
    показывает то, что человек вводил вручную — команду «Выполнить» (Win+R) и
    пути, набранные в адресной строке проводника."""
    if not HAS_WINREG:
        return []

    flagged_keywords = [k.lower() for k in config.get("flagged_file_keywords", []) if k.strip()]
    flagged_names = {n.lower() for n in config.get("flagged_file_names", [])} | {
        n.lower() for n in config.get("flagged_process_names", [])
    }

    def is_flagged(text: str) -> bool:
        lt = text.lower()
        return any(n in lt for n in flagged_names) or any(kw in lt for kw in flagged_keywords)

    targets = [
        ("Выполнить (Win+R)", r"Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU"),
        ("Введённые пути в проводнике", r"Software\Microsoft\Windows\CurrentVersion\Explorer\TypedPaths"),
    ]
    results: list[MRUEntry] = []
    for label, key_path in targets:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name.upper() == "MRULIST" or not str(value).strip():
                    continue
                text = str(value)
                results.append(MRUEntry(source=label, value=text, flagged=is_flagged(text)))
        finally:
            winreg.CloseKey(key)
    return results


def find_everything_install() -> str:
    """Определяет, установлен ли voidtools Everything — если да, для быстрого
    поиска файлов удобнее пользоваться им напрямую (у него свой файловый индекс).
    Наш «Скан диска» и без него проверяет буквально все файлы, просто медленнее
    на очень больших дисках, так как не использует индекс."""
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Everything" / "Everything.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Everything" / "Everything.exe",
        Path(os.environ.get("LocalAppData", "")) / "Everything" / "Everything.exe",
    ]
    for c in candidates:
        if c.exists():
            return "installed"
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == "everything.exe":
                return "running"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return "not_found"


# ---------------------------------------------------------------------------
# JSON-сериализация — мост pywebview передаёт в JS только простые типы,
# datetime переводим в ISO-строку (или None), dataclass -> dict рекурсивно.
# ---------------------------------------------------------------------------

def to_jsonable(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return str(obj)


