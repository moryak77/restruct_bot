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


def setup(bot: commands.InteractionBot):
    bot.add_cog(AutoRole(bot))
