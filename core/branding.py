from __future__ import annotations

import datetime as _dt
from pathlib import Path

import disnake

from core.config import config

FAMILY_NAME = config.get("family_name", "RESTRUCT")

PANEL_IMAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "panel_images"
PANEL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def _brand_color() -> disnake.Color:
    raw = config.get("brand_color", "0x8B0000")
    try:
        return disnake.Color(int(str(raw), 16) if str(raw).lower().startswith("0x") else int(raw))
    except (TypeError, ValueError):
        return disnake.Color(0x8B0000)


BRAND_COLOR = _brand_color()

_ATTACHMENT_NAME = "panel_banner"


def _panel_image_path(panel_key: str) -> Path | None:
    matches = list(PANEL_IMAGE_DIR.glob(f"{panel_key}.*"))
    return matches[0] if matches else None


def save_panel_image(panel_key: str, filename: str, data: bytes) -> None:
    """Сохраняет картинку панели локально, чтобы переприкреплять её свежей копией каждый раз
    (ссылки Discord CDN на вложения истекают, поэтому хранить голый URL нельзя)."""
    for old in PANEL_IMAGE_DIR.glob(f"{panel_key}.*"):
        old.unlink(missing_ok=True)
    ext = Path(filename).suffix.lower() or ".png"
    (PANEL_IMAGE_DIR / f"{panel_key}{ext}").write_bytes(data)


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
        color=color or BRAND_COLOR,
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
