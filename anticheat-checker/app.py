"""
RESTRUCT — Проверка на читы (GTA5RP): точка входа.

Окно — обычный локальный webview (движок Edge/WebView2, уже стоит в Windows),
рендерит web/index.html. Вся логика проверки — в backend.py, здесь только
мост между JS и Python. Никаких сетевых запросов ни на одном из уровней.
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import webview

import backend

APP_TITLE = "Restruct - Checker"


def _base_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def _web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", _base_dir())) / "web"
    return _base_dir() / "web"


class Api:
    def __init__(self):
        self.window: webview.Window | None = None
        self._disk_stop = threading.Event()
        self._disk_thread: threading.Thread | None = None

    # -- служебное --------------------------------------------------------

    def get_config(self) -> dict:
        cfg = backend.load_config()
        return {
            "logDir": str(backend._log_dir()),
            "everything": backend.find_everything_install(),
            "roots": [str(r) for r in backend.get_scan_roots()],
        }

    # -- «Проверка» ---------------------------------------------------------

    def run_scan(self) -> dict:
        cfg = backend.load_config()
        result = backend.run_full_scan(cfg)
        payload = backend.to_jsonable(result)
        report = _build_scan_report_text(result)
        backend.write_log("scan", report)
        payload["reportText"] = report
        return payload

    # -- «Все процессы» ------------------------------------------------------

    def get_processes(self) -> list[dict]:
        cfg = backend.load_config()
        return backend.to_jsonable(backend.list_all_processes(cfg))

    def get_process_detail(self, pid: int) -> dict:
        import psutil

        try:
            p = psutil.Process(pid)
            parent = p.parent()
            return {
                "ok": True,
                "parent": f"{parent.name()} (PID {parent.pid})" if parent else "—",
                "started": _fmt_dt(p.create_time()),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_signature(self, path: str) -> str:
        return backend.check_signature(path)

    # -- «Скан диска» --------------------------------------------------------

    def start_disk_scan(self) -> bool:
        if self._disk_thread is not None and self._disk_thread.is_alive():
            return False
        cfg = backend.load_config()
        self._disk_stop = threading.Event()
        result_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        def worker():
            backend.scan_full_disk(cfg, self._disk_stop, result_queue)

        def pump():
            matches_all = []
            while True:
                kind, payload = result_queue.get()
                if kind == "progress":
                    self._eval(f"window.onDiskProgress && window.onDiskProgress({payload})")
                elif kind == "match":
                    finding = backend.to_jsonable(payload)
                    matches_all.append(finding)
                    self._eval(f"window.onDiskMatch && window.onDiskMatch({_js(finding)})")
                elif kind == "done":
                    scanned, matches = payload
                    report = _build_disk_report_text(scanned, matches)
                    backend.write_log("disk_scan", report)
                    self._eval(
                        f"window.onDiskDone && window.onDiskDone({scanned}, {_js(report)})"
                    )
                    break

        self._disk_thread = threading.Thread(target=worker, daemon=True)
        self._disk_thread.start()
        threading.Thread(target=pump, daemon=True).start()
        return True

    def stop_disk_scan(self) -> bool:
        self._disk_stop.set()
        return True

    # -- «Корзина» -----------------------------------------------------------

    def get_recycle_bin(self) -> dict:
        cfg = backend.load_config()
        items = backend.scan_recycle_bin(cfg)
        report = _build_recycle_report_text(items)
        backend.write_log("recycle_bin", report)
        return {"items": backend.to_jsonable(items)}

    # -- «Активность» --------------------------------------------------------

    def get_activity(self) -> dict:
        cfg = backend.load_config()
        prefetch, err = backend.list_prefetch_activity(cfg)
        mru = backend.list_recent_mru(cfg)
        report = _build_activity_report_text(prefetch, mru)
        backend.write_log("activity", report)
        return {
            "prefetch": backend.to_jsonable(prefetch),
            "prefetchError": err,
            "mru": backend.to_jsonable(mru),
        }

    # -- «Логи» ---------------------------------------------------------------

    def get_logs(self) -> list[dict]:
        logs = backend.list_logs()
        return [
            {"name": p.name, "stem": p.stem, "mtime": _fmt_dt(p.stat().st_mtime)}
            for p in logs
        ]

    def read_log(self, name: str) -> str:
        path = backend._log_dir() / name
        try:
            path.resolve().relative_to(backend._log_dir().resolve())
        except ValueError:
            return "Недопустимый путь."
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Не удалось открыть файл: {exc}"

    # -- утилиты ---------------------------------------------------------------

    def _eval(self, js: str) -> None:
        if self.window is not None:
            try:
                self.window.evaluate_js(js)
            except Exception:
                pass


def _fmt_dt(value) -> str:
    from datetime import datetime

    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value)
    return value.strftime("%d.%m.%Y %H:%M:%S")


def _js(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _build_scan_report_text(result) -> str:
    from datetime import datetime

    lines = [f"RESTRUCT — отчёт проверки на читы ({datetime.now().strftime('%H:%M:%S')})", "-" * 58]
    lines.append(f"Всего процессов на компьютере: {len(result.all_processes)}")
    lines.append("")
    if result.flagged_processes:
        lines.append(f"НАЙДЕНЫ ПОДОЗРИТЕЛЬНЫЕ ПРОЦЕССЫ ({len(result.flagged_processes)}):")
        for p in result.flagged_processes:
            lines.append(f"  ✗ {p.name}  (PID {p.pid})  —  {p.exe}")
    else:
        lines.append("Процессов из flagged_process_names не найдено.")
    lines.append("")
    if not result.game_process_found:
        lines.append("Процесс игры не найден среди game_process_names.")
    else:
        lines.append(f"Игровой процесс: {result.game_process_name}")
        if result.module_scan_error:
            lines.append(f"⚠ {result.module_scan_error}")
        elif result.flagged_modules:
            lines.append(f"ПОДОЗРИТЕЛЬНЫЕ МОДУЛИ В ИГРЕ ({len(result.flagged_modules)}):")
            for m in result.flagged_modules:
                lines.append(f"  ✗ {m.path}")
        else:
            lines.append("Модулей из подозрительных папок в игре не найдено.")
    lines.append("")
    lines.append("Это не окончательный вердикт — решение принимает администратор.")
    return "\n".join(lines)


def _build_disk_report_text(scanned: int, matches) -> str:
    from datetime import datetime

    lines = [
        f"RESTRUCT — отчёт скана диска ({datetime.now().strftime('%H:%M:%S')})",
        f"Просканировано файлов: {scanned}",
        f"Найдено совпадений: {len(matches)}",
        "",
    ]
    for m in matches:
        lines.append(f"✗ {m.path}  ({m.matched_rule})")
    return "\n".join(lines)


def _build_recycle_report_text(items) -> str:
    from datetime import datetime

    lines = [f"RESTRUCT — отчёт по корзине ({datetime.now().strftime('%H:%M:%S')})", f"Всего элементов: {len(items)}", ""]
    for item in items:
        when = item.deleted_at.strftime("%d.%m.%Y %H:%M:%S") if item.deleted_at else "неизвестно"
        mark = "⚠" if item.flagged else " "
        lines.append(f"{mark} [{when}] {item.original_path} ({item.size} байт)")
    return "\n".join(lines)


def _build_activity_report_text(prefetch, mru) -> str:
    from datetime import datetime

    lines = [f"RESTRUCT — отчёт активности ({datetime.now().strftime('%H:%M:%S')})", "", "Prefetch:"]
    for e in prefetch:
        when = e.last_run.strftime("%d.%m.%Y %H:%M:%S") if e.last_run else "неизвестно"
        lines.append(f"{'⚠' if e.flagged else ' '} [{when}] {e.exe_name}")
    lines.append("")
    lines.append("MRU:")
    for m in mru:
        lines.append(f"{'⚠' if m.flagged else ' '} [{m.source}] {m.value}")
    return "\n".join(lines)


def main():
    api = Api()
    window = webview.create_window(
        APP_TITLE,
        str(_web_dir() / "index.html"),
        js_api=api,
        width=1200,
        height=780,
        min_size=(980, 620),
        background_color="#0a0a0c",
    )
    api.window = window
    webview.start(debug=False)


if __name__ == "__main__":
    main()
