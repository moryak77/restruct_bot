from __future__ import annotations

import asyncio
import datetime as dt
import logging

from core.config import config
from core.http import get_session

log = logging.getLogger("restruct-bot")

_BATCH_SIZE = 200


def _site_base_url() -> str:
    return (config.get("verification.site_base_url") or "").rstrip("/")


def _api_secret() -> str:
    return config.get("verification.api_secret") or ""


async def post_events(events: list[dict]) -> None:
    """Отправляет события (профиль, войс, сообщения, наказания) на сайт. Сбой сети не должен
    ронять бота — просто пишем предупреждение."""
    if not events or not _api_secret() or not _site_base_url():
        return
    session = get_session()
    # Батчами по _BATCH_SIZE — сервер на 4000+ участников не влезет в один запрос.
    for i in range(0, len(events), _BATCH_SIZE):
        chunk = events[i : i + _BATCH_SIZE]
        try:
            async with session.post(
                f"{_site_base_url()}/api/discord/sync",
                headers={"X-Api-Key": _api_secret()},
                json={"events": chunk},
                timeout=20,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("Синк с сайтом вернул %s: %s", resp.status, body[:300])
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось отправить синк на сайт: %s", e)


def report_punishment(
    discord_id: int,
    kind: str,
    reason: str | None,
    moderator: str,
    until: dt.datetime | None = None,
) -> None:
    """Сообщает сайту о наказании (warn/mute/kick/ban) — оно показывается в статистике профиля.
    Запускается в фоне и не блокирует команду модератора."""
    event = {
        "type": "punishment",
        "discordId": str(discord_id),
        "kind": kind,
        "reason": (reason or "")[:300],
        "moderator": moderator[:80],
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "until": until.isoformat() if until else None,
    }
    try:
        asyncio.get_running_loop().create_task(post_events([event]))
    except RuntimeError:
        pass
