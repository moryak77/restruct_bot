from __future__ import annotations

import disnake

# Собственные серые application-эмодзи бота. Обычные юникод-эмодзи цветные "из коробки"
# и перекрасить их нельзя — поэтому весь интерфейс использует загруженные плоские
# PNG-иконки (Tabler Icons / Phosphor Icons, обе MIT) в едином сером тоне.
ICONS: dict[str, disnake.PartialEmoji] = {
    "lock": disnake.PartialEmoji(name="voice_lock", id=1538631844558282903),
    "increase": disnake.PartialEmoji(name="voice_increase", id=1538631873750638593),
    "decrease": disnake.PartialEmoji(name="voice_decrease", id=1538631879803019465),
    "kick": disnake.PartialEmoji(name="voice_kick", id=1538631886321225800),
    "gavel": disnake.PartialEmoji(name="icon_gavel", id=1538683046042017854),
    "ban": disnake.PartialEmoji(name="icon_ban", id=1538683048374042764),
    "delete": disnake.PartialEmoji(name="voice_delete", id=1538631906080456854),
    "check": disnake.PartialEmoji(name="icon_check", id=1538631911977525339),
    "cross": disnake.PartialEmoji(name="icon_cross", id=1538631917539434568),
    "hand": disnake.PartialEmoji(name="icon_hand", id=1538631923876888617),
    "shield": disnake.PartialEmoji(name="icon_shield", id=1538631931091222558),
    "moon": disnake.PartialEmoji(name="icon_moon", id=1538631936858259466),
    "beach": disnake.PartialEmoji(name="icon_beach", id=1538631942654664794),
    "pending": disnake.PartialEmoji(name="icon_pending", id=1538631948669558945),
    "unassigned": disnake.PartialEmoji(name="icon_unassigned", id=1538631954595971102),
    "clipboard": disnake.PartialEmoji(name="icon_clipboard", id=1538631960627253339),
    "ticket": disnake.PartialEmoji(name="icon_ticket", id=1538631966742675537),
    "graduation": disnake.PartialEmoji(name="icon_graduation", id=1538631974237900852),
    "help": disnake.PartialEmoji(name="icon_help", id=1538631980009132136),
    "alert": disnake.PartialEmoji(name="icon_alert", id=1538631986401509457),
    "tool": disnake.PartialEmoji(name="icon_tool", id=1538631992286126151),
    "package": disnake.PartialEmoji(name="icon_package", id=1538631998556475513),
    "settings": disnake.PartialEmoji(name="icon_settings", id=1538632004567044118),
    "send": disnake.PartialEmoji(name="voice_transfer", id=1538631899583610940),
    "car": disnake.PartialEmoji(name="icon_car", id=1538632009944010772),
    "key": disnake.PartialEmoji(name="icon_key", id=1538632016344649879),
    "bank": disnake.PartialEmoji(name="icon_bank", id=1538632022136852593),
    "coin": disnake.PartialEmoji(name="icon_coin", id=1538632028952465500),
    "users": disnake.PartialEmoji(name="role_users", id=1538632035755630603),
    "announce": disnake.PartialEmoji(name="icon_announce", id=1539655432644067430),
    "bind": disnake.PartialEmoji(name="icon_bind", id=1539655434741354566),
    "tag": disnake.PartialEmoji(name="icon_tag", id=1539655437010599947),
    "plus": disnake.PartialEmoji(name="icon_plus", id=1539655439094906980),
    "back": disnake.PartialEmoji(name="icon_back", id=1539655440974086255),
    "smile": disnake.PartialEmoji(name="icon_smile", id=1539655443322904707),
    "save": disnake.PartialEmoji(name="icon_save", id=1539655445352943617),
    "link": disnake.PartialEmoji(name="icon_link", id=1539655447823261716),
    "pencil": disnake.PartialEmoji(name="icon_pencil", id=1539655449727471670),
    "palette": disnake.PartialEmoji(name="icon_palette", id=1539661310856532018),
}


def icon(key: str) -> disnake.PartialEmoji:
    """Эмодзи для параметра emoji= у кнопок/опций."""
    return ICONS[key]


def icon_tag(key: str) -> str:
    """Строковый тег <:name:id> для вставки иконки в текст embed."""
    e = ICONS[key]
    return f"<:{e.name}:{e.id}>"
