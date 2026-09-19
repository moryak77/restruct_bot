from __future__ import annotations

import asyncio
import atexit
import os
import threading
from typing import Any

import aiohttp

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
        _session = aiohttp.ClientSession()
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


def execute_nowait(sql: str, args: list[Any] | None = None) -> None:
    """Запустить запрос в фоне, не дожидаясь ответа (используется в save())."""
    asyncio.run_coroutine_threadsafe(_execute_async([(sql, args)]), _loop)


async def _close_session() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


def _shutdown() -> None:
    if _loop.is_running():
        asyncio.run_coroutine_threadsafe(_close_session(), _loop).result(timeout=5)
        _loop.call_soon_threadsafe(_loop.stop)


atexit.register(_shutdown)
