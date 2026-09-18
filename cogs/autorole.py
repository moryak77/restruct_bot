from __future__ import annotations

import logging

import disnake
from disnake.ext import commands

from core.config import config

log = logging.getLogger("restruct-bot")


def _autorole_ids() -> list[int]:
    return [i for i in (config.get("autorole.role_ids", []) or []) if i]


class AutoRole(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        if member.bot:
            return

        roles = [member.guild.get_role(role_id) for role_id in _autorole_ids()]
        roles = [role for role in roles if role is not None]
        if not roles:
            return

        try:
            await member.add_roles(*roles, reason="Автовыдача роли при входе на сервер")
        except disnake.Forbidden:
            log.warning(
                "Не удалось выдать автороль(и) участнику %s — роль бота ниже нужной роли в иерархии.",
                member,
            )

    @commands.slash_command(
        name="autorole",
        description="Массовая выдача автороли",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def autorole(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @autorole.sub_command(
        name="apply_all",
        description="Выдать настроенную в config.json автороль(и) всем участникам, у кого их ещё нет",
    )
    async def autorole_apply_all(self, inter: disnake.ApplicationCommandInteraction):
        roles = [inter.guild.get_role(role_id) for role_id in _autorole_ids()]
        roles = [role for role in roles if role is not None]
        if not roles:
            await inter.response.send_message(
                "Автороль не настроена — заполни `autorole.role_ids` в config.json.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        updated = 0
        skipped = 0
        failed = 0
        for member in inter.guild.members:
            if member.bot:
                continue
            missing = [role for role in roles if role not in member.roles]
            if not missing:
                skipped += 1
                continue
            try:
                await member.add_roles(
                    *missing, reason=f"Массовая выдача автороли ({inter.author})"
                )
                updated += 1
            except disnake.HTTPException:
                failed += 1

        summary = f"Готово: выдано **{updated}**, уже было у **{skipped}**"
        if failed:
            summary += f", не удалось у **{failed}** (роль бота ниже в иерархии?)"
        await inter.edit_original_response(content=summary + ".")


def setup(bot: commands.InteractionBot):
    bot.add_cog(AutoRole(bot))
