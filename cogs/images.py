from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import save_panel_image

PANEL_CHOICES = {
    "welcome": "Приветствие новых участников (авто при входе)",
    "cars": "Автопарк (/cars show)",
    "bank": "Банк семьи (/bank)",
    "afk": "AFK и отпуск (/afk)",
    "curator": "Тикеты куратора (/curator)",
    "recruit": "Тикеты рекрута (/recruit)",
    "general": "Основная панель тикетов (/ticket)",
    "moderation": "Активные заявки (/moderation)",
    "punishment": "Заявка на наказание (/punishment show)",
    "voice": "Управление голосовыми каналами (/voice)",
    "vzp": "Колл на ВЗП (/vzp)",
    "news": "Баннер новостей (/news, если не приложена своя картинка)",
    "contracts": "Контракты семьи (/contract)",
    "music": "Музыкальная панель (/music)",
    "redux": "Каталог редуксов (/redux)",
    "rules": "Правила сервера (/rules show)",
    "shop": "Магазин услуг (/shop show)",
}

# Param(choices=...) ожидает {отображаемое_имя: значение}, поэтому меняем местами ключ/значение.
PANEL_OPTION_CHOICES = {label: key for key, label in PANEL_CHOICES.items()}


class Images(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="img",
        description="Изменить картинку указанной панели",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def img(
        self,
        inter: disnake.ApplicationCommandInteraction,
        panel: str = commands.Param(choices=PANEL_OPTION_CHOICES, description="Какую панель изменить"),
        image: disnake.Attachment = commands.Param(description="Новое изображение панели"),
    ):
        is_image_type = (image.content_type or "").startswith("image/")
        is_image_ext = image.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        if not (is_image_type or is_image_ext):
            await inter.response.send_message("❌ Загруженный файл не является изображением.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        data = await image.read()
        save_panel_image(panel, image.filename, data)
        await inter.followup.send(
            f"✅ Картинка панели **{PANEL_CHOICES[panel]}** обновлена.\n"
            f"Чтобы изменения вступили в силу, заново вызовите соответствующую команду панели.",
            ephemeral=True,
        )


def setup(bot: commands.InteractionBot):
    bot.add_cog(Images(bot))
