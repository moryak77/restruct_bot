from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config
from core.icons import icon
from core.storage import vzp_store

ADMIN_PERMS = disnake.Permissions(manage_guild=True)


def _positions() -> dict[str, int]:
    return config.get("vzp.positions", {}) or {}


def _build_embed(checked_in: dict[str, list[str]]) -> disnake.Embed:
    embed = base_embed(
        "📢 Колл сотрудников на ВЗП",
        "Все сотрудники обязаны отметиться, нажав кнопку ниже, в соответствии со своей должностью.",
        panel_key="vzp",
    )
    for name in _positions():
        members = [uid for uid, positions in checked_in.items() if name in positions]
        value = "\n".join(f"• <@{uid}>" for uid in members) if members else "—"
        embed.add_field(name=f"{name} ({len(members)})", value=value, inline=True)
    return embed


class VzpCheckinView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Я на месте", emoji=icon("check"), style=disnake.ButtonStyle.secondary, custom_id="vzp_checkin")
    async def checkin(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        member_role_ids = {role.id for role in inter.author.roles}
        matched = [name for name, role_id in _positions().items() if role_id and role_id in member_role_ids]

        if not matched:
            await inter.response.send_message(
                "❌ За вами не закреплена ни одна из должностей, участвующих в этом колле.",
                ephemeral=True,
            )
            return

        data = vzp_store.load()
        session = data["sessions"].setdefault(str(inter.message.id), {"checked_in": {}})
        checked_in = session["checked_in"]
        user_key = str(inter.author.id)

        if user_key in checked_in:
            del checked_in[user_key]
        else:
            checked_in[user_key] = matched

        vzp_store.save(data)
        await inter.response.edit_message(embed=_build_embed(checked_in))


class Vzp(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="vzp",
        description="Запустить колл сотрудников по позициям на ВЗП",
        default_member_permissions=ADMIN_PERMS,
    )
    async def vzp(self, inter: disnake.ApplicationCommandInteraction):
        positions = _positions()
        if not positions:
            await inter.response.send_message(
                "❌ Должности для ВЗП не настроены. Добавьте их в config.json (vzp.positions).",
                ephemeral=True,
            )
            return

        embed = _build_embed({})
        role_mentions = " ".join(f"<@&{role_id}>" for role_id in positions.values() if role_id)

        message = await send_panel(
            inter,
            embed,
            view=VzpCheckinView(),
            panel_key="vzp",
            content=role_mentions or None,
            allowed_mentions=disnake.AllowedMentions(roles=True),
        )

        data = vzp_store.load()
        data["sessions"][str(message.id)] = {"checked_in": {}}
        vzp_store.save(data)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Vzp(bot))


PERSISTENT_VIEWS = [lambda: VzpCheckinView()]
