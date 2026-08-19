from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.icons import icon_tag
from core.storage import rules_store

ADMIN_PERMS = disnake.Permissions(manage_guild=True)


def _build_rules_embed() -> disnake.Embed:
    rules = rules_store.load()["rules"]

    intro = (
        "Соблюдение правил обязательно для всех участников сервера. За нарушение — "
        "предупреждение, мут, кик или бан, в зависимости от тяжести проступка, на "
        "усмотрение модерации.\n\n"
        f"{icon_tag('cross')} **Запрещено:**\n\n"
    )
    rules_text = "\n\n".join(f"**{i + 1}.** {rule}" for i, rule in enumerate(rules)) or "Правила пока не заданы."

    embed = base_embed(f"{icon_tag('shield')} Правила семьи RESTRUCT", panel_key="rules")
    embed.description = (intro + rules_text)[:4096]
    embed.add_field(
        name=f"{icon_tag('alert')} Важно",
        value="Незнание правил не освобождает от ответственности за их нарушение.",
        inline=False,
    )
    return embed


async def _refresh_rules_panel(guild: disnake.Guild) -> None:
    data = rules_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=_build_rules_embed())
    except disnake.HTTPException:
        pass


class EditRulesModal(disnake.ui.Modal):
    def __init__(self, current_rules: list[str]):
        components = [
            disnake.ui.TextInput(
                label="Правила (каждое — с новой строки)",
                custom_id="rules_text",
                style=disnake.TextInputStyle.paragraph,
                value="\n".join(current_rules)[:4000],
                max_length=4000,
            ),
        ]
        super().__init__(title="Редактирование правил", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        new_rules = [line.strip() for line in inter.text_values["rules_text"].splitlines() if line.strip()]
        if not new_rules:
            await inter.response.send_message("❌ Список правил не может быть пустым.", ephemeral=True)
            return

        data = rules_store.load()
        data["rules"] = new_rules
        rules_store.save(data)

        await inter.response.send_message(f"✅ Правила обновлены — теперь их {len(new_rules)}.", ephemeral=True)
        await _refresh_rules_panel(inter.guild)


class Rules(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="rules",
        description="Правила сервера",
        default_member_permissions=ADMIN_PERMS,
    )
    async def rules(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @rules.sub_command(name="show", description="Опубликовать панель с правилами сервера")
    async def rules_show(self, inter: disnake.ApplicationCommandInteraction):
        message = await send_panel(inter, _build_rules_embed(), panel_key="rules")

        data = rules_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        rules_store.save(data)

    @rules.sub_command(name="edit", description="Изменить текст правил")
    async def rules_edit(self, inter: disnake.ApplicationCommandInteraction):
        current_rules = rules_store.load()["rules"]
        await inter.response.send_modal(EditRulesModal(current_rules))


def setup(bot: commands.InteractionBot):
    bot.add_cog(Rules(bot))
