from __future__ import annotations

import aiohttp

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """Общая aiohttp-сессия на весь процесс — переиспользует TCP/TLS-соединения вместо
    пересоздания на каждый запрос к ЮKassa/ЮMoney/Spotify. Создаётся лениво при первом
    обращении (к этому моменту event loop уже точно запущен)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session
