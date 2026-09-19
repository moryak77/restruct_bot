from __future__ import annotations

import asyncio
import atexit
import itertools
import logging
import os
import threading
from typing import Any

import aiohttp

log = logging.getLogger("restruct-bot")

_RAW_URL = os.environ.get("TURSO_DATABASE_URL", "")
_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


def _pipeline_url() -> str:
    # libsql://host -> https://host/v2/pipeline (Turso SQL-over-HTTP)
    host = _RAW_URL.split("://", 1)[-1]
    return f"https://{host}/v2/pipeline"


_PIPELINE_URL = _pipeline_url()

# Отдельный поток с собственным event loop — весь HTTP к Turso идёт через него, не завязан
# на жизненный цикл основного event loop бота и не блокирует его синхронными сетевыми вызовами.
_loop = asyncio.new_event_loop()
_thread = threading.Thread(target=_loop.run_forever, name="turso-io", daemon=True)
_thread.start()

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


def _sql_arg(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


async def _execute_async(statements: list[tuple[str, list[Any] | None]]) -> list[dict]:
    if not _RAW_URL or not _TOKEN:
        raise RuntimeError("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN не заданы в окружении")
    session = await _get_session()
    requests: list[dict] = []
    for sql, args in statements:
        stmt: dict[str, Any] = {"sql": sql}
        if args:
            stmt["args"] = [_sql_arg(a) for a in args]
        requests.append({"type": "execute", "stmt": stmt})
    requests.append({"type": "close"})
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with session.post(_PIPELINE_URL, json={"requests": requests}, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = data["results"]
    for r in results:
        if r.get("type") == "error":
            raise RuntimeError(f"Turso error: {r.get('error')}")
    return results


def execute_blocking(statements: list[tuple[str, list[Any] | None]]) -> list[dict]:
    """Синхронно выполнить запрос(ы) и дождаться результата. Только для старта — до того,
    как в основном потоке появится собственный event loop бота."""
    future = asyncio.run_coroutine_threadsafe(_execute_async(statements), _loop)
    return future.result(timeout=20)


# ── Очередь фоновых записей ────────────────────────────────────────────────────────────
# Раньше каждый save() запускал независимую задачу: при нескольких быстрых сохранениях
# одного ключа более старый снимок мог долететь до Turso ПОЗЖЕ нового и затереть его
# (после рестарта «воскресали» погашенные коды, откатывались тикеты), а неудачная запись
# (сетевой сбой) терялась молча. Теперь запись идёт одним воркером строго по порядку:
#   • по каждому ключу в очереди хранится только самый свежий снимок (coalescing),
#   • всё накопленное уходит одним HTTP-запросом (пачкой),
#   • при сбое — повторы с паузой, данные не теряются, пока процесс жив,
#   • при остановке процесса очередь досбрасывается (atexit).
_pending: dict[str, tuple[str, list[Any] | None]] = {}
_worker_task: asyncio.Task | None = None
_anon_keys = itertools.count()


async def _worker() -> None:
    global _worker_task
    try:
        while _pending:
            batch = list(_pending.items())
            _pending.clear()
            try:
                await _execute_async([stmt for _, stmt in batch])
            except Exception as e:  # noqa: BLE001
                log.error("Запись в Turso не удалась (%s), повтор через 3с", e)
                # Возвращаем в очередь, но не затираем более свежие снимки того же ключа.
                for key, stmt in batch:
                    _pending.setdefault(key, stmt)
                await asyncio.sleep(3)
    finally:
        _worker_task = None
        if _pending:  # что-то успело прилететь между последней проверкой и выходом
            _worker_task = asyncio.ensure_future(_worker())


async def _enqueue(key: str, sql: str, args: list[Any] | None) -> None:
    global _worker_task
    _pending[key] = (sql, args)
    if _worker_task is None:
        _worker_task = asyncio.ensure_future(_worker())


def execute_nowait(sql: str, args: list[Any] | None = None, key: str | None = None) -> None:
    """Поставить запись в очередь и вернуться сразу (используется в save()).
    Записи с одним `key` схлопываются — в Turso уходит только последнее состояние."""
    if key is None:
        key = f"__anon_{next(_anon_keys)}"
    asyncio.run_coroutine_threadsafe(_enqueue(key, sql, args), _loop)


async def _drain() -> None:
    while _worker_task is not None or _pending:
        task = _worker_task
        if task is None:
            await asyncio.sleep(0.05)
            continue
        await asyncio.shield(task)


def flush_blocking(timeout: float = 10.0) -> None:
    """Дождаться, пока все накопленные записи уйдут в Turso."""
    if not _loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_drain(), _loop).result(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось досбросить очередь записи в Turso при остановке: %s", e)


async def _close_session() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


def _shutdown() -> None:
    if _loop.is_running():
        flush_blocking()
        try:
            asyncio.run_coroutine_threadsafe(_close_session(), _loop).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        _loop.call_soon_threadsafe(_loop.stop)


atexit.register(_shutdown)
