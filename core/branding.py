from __future__ import annotations

import base64
import datetime as _dt
import logging
from pathlib import Path

import disnake

from core.config import config
from core.turso_http import execute_blocking, execute_nowait

_log = logging.getLogger("restruct-bot")

FAMILY_NAME = config.get("family_name", "RESTRUCT")

PANEL_IMAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "panel_images"
PANEL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# Единый фирменный цвет полоски у всех embed бота — ярко-синий. Раньше брался из
# config.brand_color (серый), а отдельные сообщения красили полоску в красный/зелёный/оранжевый.
BRAND_COLOR = disnake.Color(0x2F7BFF)


_ATTACHMENT_NAME = "panel_banner"


def _panel_image_path(panel_key: str) -> Path | None:
    matches = list(PANEL_IMAGE_DIR.glob(f"{panel_key}.*"))
    return matches[0] if matches else None


_PANEL_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS panel_images "
    "(key TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL, data TEXT NOT NULL)"
)


def save_panel_image(panel_key: str, filename: str, data: bytes) -> None:
    """Сохраняет картинку панели локально, чтобы переприкреплять её свежей копией каждый раз
    (ссылки Discord CDN на вложения истекают, поэтому хранить голый URL нельзя), и дублирует
    в Turso: на Render диск контейнера стирается при каждом деплое, и без копии в базе всё,
    что загружено через /img, пропадало бы."""
    for old in PANEL_IMAGE_DIR.glob(f"{panel_key}.*"):
        old.unlink(missing_ok=True)
    ext = Path(filename).suffix.lower() or ".png"
    (PANEL_IMAGE_DIR / f"{panel_key}{ext}").write_bytes(data)
    execute_nowait(
        "INSERT INTO panel_images (key, filename, size, data) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET filename = excluded.filename, size = excluded.size, data = excluded.data",
        [panel_key, f"{panel_key}{ext}", len(data), base64.b64encode(data).decode()],
        key=f"panelimg:{panel_key}",
    )


def _restore_panel_images() -> None:
    """При старте подтягивает из Turso картинки, загруженные через /img, если локальная
    копия отсутствует или отличается (после деплоя на Render локальных файлов нет)."""
    try:
        rows = execute_blocking(
            [(_PANEL_TABLE_SQL, None), ("SELECT key, filename, size FROM panel_images", None)]
        )[1]["response"]["result"].get("rows", [])
        restored = 0
        for row in rows:
            key, filename, size = row[0]["value"], row[1]["value"], int(row[2]["value"])
            local = _panel_image_path(key)
            if local is not None and local.name == filename and local.stat().st_size == size:
                continue
            data_rows = execute_blocking([("SELECT data FROM panel_images WHERE key = ?", [key])])[0][
                "response"
            ]["result"].get("rows", [])
            if not data_rows:
                continue
            for old in PANEL_IMAGE_DIR.glob(f"{key}.*"):
                old.unlink(missing_ok=True)
            (PANEL_IMAGE_DIR / filename).write_bytes(base64.b64decode(data_rows[0][0]["value"]))
            restored += 1
        if restored:
            _log.info("Восстановлено картинок панелей из Turso: %s", restored)
    except Exception as e:  # noqa: BLE001 — без базы бот стартует с тем, что лежит на диске
        _log.warning("Не удалось восстановить картинки панелей из Turso: %s", e)


_restore_panel_images()


def panel_file_kwargs(panel_key: str | None) -> dict:
    """Возвращает {"file": disnake.File(...)} если для панели задана картинка, иначе {}.
    Распакуйте через ** в send_message/edit_message вместе с embed от base_embed(panel_key=...)."""
    if not panel_key:
        return {}
    path = _panel_image_path(panel_key)
    if path is None:
        return {}
    return {"file": disnake.File(path, filename=f"{_ATTACHMENT_NAME}{path.suffix}")}


def base_embed(
    title: str,
    description: str | None = None,
    *,
    color: disnake.Color | None = None,
    panel_key: str | None = None,
    timestamp: bool = False,
    footer_text: str | None = None,
) -> disnake.Embed:
    embed = disnake.Embed(
        title=title,
        description=description,
        color=BRAND_COLOR,  # параметр color оставлен для совместимости вызовов, цвет всегда фирменный
        timestamp=_dt.datetime.now(_dt.timezone.utc) if timestamp else None,
    )
    footer_icon = config.get("footer_icon_url") or None
    embed.set_footer(text=footer_text or f"Семья {FAMILY_NAME}", icon_url=footer_icon)

    if panel_key:
        path = _panel_image_path(panel_key)
        if path is not None:
            embed.set_image(url=f"attachment://{_ATTACHMENT_NAME}{path.suffix}")

    return embed


async def send_panel(
    inter: disnake.ApplicationCommandInteraction,
    embed: disnake.Embed,
    *,
    view: disnake.ui.View | None = None,
    panel_key: str | None = None,
    content: str | None = None,
    allowed_mentions: disnake.AllowedMentions | None = None,
) -> disnake.Message:
    """Публикует панель обычным сообщением бота в канал вместо прямого ответа на команду —
    иначе над панелью навсегда висела бы пометка Discord «имярек использует /команда»."""
    await inter.response.send_message("✅ Панель опубликована в этом канале.", ephemeral=True)
    return await inter.channel.send(
        content=content,
        embed=embed,
        view=view,
        allowed_mentions=allowed_mentions,
        **panel_file_kwargs(panel_key),
    )
