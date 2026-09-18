from __future__ import annotations

import disnake

# Собственные серые application-эмодзи бота. Обычные юникод-эмодзи цветные "из коробки"
# и перекрасить их нельзя — поэтому весь интерфейс использует загруженные плоские
# PNG-иконки (Tabler Icons / Phosphor Icons, обе MIT) в едином сером тоне.
ICONS: dict[str, disnake.PartialEmoji] = {
    "lock": disnake.PartialEmoji(name="voice_lock", id=1550153172784390184),
    "increase": disnake.PartialEmoji(name="voice_increase", id=1550153176970432576),
    "decrease": disnake.PartialEmoji(name="voice_decrease", id=1550153181583908867),
    "kick": disnake.PartialEmoji(name="voice_kick", id=1550153186206290084),
    "gavel": disnake.PartialEmoji(name="icon_gavel", id=1550153190606118914),
    "ban": disnake.PartialEmoji(name="icon_ban", id=1550153194833973272),
    "delete": disnake.PartialEmoji(name="voice_delete", id=1550153199141523589),
    "check": disnake.PartialEmoji(name="icon_check", id=1550153203776094208),
    "cross": disnake.PartialEmoji(name="icon_cross", id=1550153207848640542),
    "hand": disnake.PartialEmoji(name="icon_hand", id=1550153211959058482),
    "shield": disnake.PartialEmoji(name="icon_shield", id=1550153216157687988),
    "moon": disnake.PartialEmoji(name="icon_moon", id=1550153220490535072),
    "beach": disnake.PartialEmoji(name="icon_beach", id=1550153224692965386),
    "pending": disnake.PartialEmoji(name="icon_pending", id=1550153228711235634),
    "unassigned": disnake.PartialEmoji(name="icon_unassigned", id=1550153232909729912),
    "clipboard": disnake.PartialEmoji(name="icon_clipboard", id=1550153237087129601),
    "ticket": disnake.PartialEmoji(name="icon_ticket", id=1550153241394946058),
    "graduation": disnake.PartialEmoji(name="icon_graduation", id=1550153247006654534),
    "help": disnake.PartialEmoji(name="icon_help", id=1550153251498762270),
    "alert": disnake.PartialEmoji(name="icon_alert", id=1550153255928201247),
    "tool": disnake.PartialEmoji(name="icon_tool", id=1550153260139151420),
    "package": disnake.PartialEmoji(name="icon_package", id=1550153264379723829),
    "settings": disnake.PartialEmoji(name="icon_settings", id=1550153268775231533),
    "send": disnake.PartialEmoji(name="voice_transfer", id=1550153273183436811),
    "car": disnake.PartialEmoji(name="icon_car", id=1550153278015406222),
    "key": disnake.PartialEmoji(name="icon_key", id=1550153282377228328),
    "bank": disnake.PartialEmoji(name="icon_bank", id=1550153286667993129),
    "coin": disnake.PartialEmoji(name="icon_coin", id=1550153290929676489),
    "users": disnake.PartialEmoji(name="role_users", id=1550153295031566376),
    "announce": disnake.PartialEmoji(name="icon_announce", id=1550153299146055830),
    "bind": disnake.PartialEmoji(name="icon_bind", id=1550153304028483645),
    "tag": disnake.PartialEmoji(name="icon_tag", id=1550153308331835472),
    "plus": disnake.PartialEmoji(name="icon_plus", id=1550153312329011342),
    "back": disnake.PartialEmoji(name="icon_back", id=1550153316447555684),
    "smile": disnake.PartialEmoji(name="icon_smile", id=1550153320524550304),
    "save": disnake.PartialEmoji(name="icon_save", id=1550153324660129902),
    "link": disnake.PartialEmoji(name="icon_link", id=1550153328846176286),
    "pencil": disnake.PartialEmoji(name="icon_pencil", id=1550153332868513942),
    "palette": disnake.PartialEmoji(name="icon_palette", id=1550153337377128458),
}


def icon(key: str) -> disnake.PartialEmoji:
    """Эмодзи для параметра emoji= у кнопок/опций."""
    return ICONS[key]


def icon_tag(key: str) -> str:
    """Строковый тег <:name:id> для вставки иконки в текст embed."""
    e = ICONS[key]
    return f"<:{e.name}:{e.id}>"
