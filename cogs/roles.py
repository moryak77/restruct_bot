from __future__ import annotations

import disnake
from disnake.ext import commands

from cogs.moderation import _is_leader
from core.branding import base_embed
from core.config import config
from core.icons import icon_tag

MAX_ROLES_IN_SELECT = 25  # предел Discord для select-меню


def _status_embed(icon_key: str, title: str, description: str | None = None, *, color: disnake.Color | None = None) -> disnake.Embed:
    return base_embed(f"{icon_tag(icon_key)} {title}", description, color=color)


def _member_line(member: disnake.abc.User) -> str:
    return f"{member.mention}\n`{member}`"


def _build_change_embed(
    title: str,
    member: disnake.Member,
    added: list[disnake.Role],
    removed: list[disnake.Role],
    executor: disnake.abc.User,
    *,
    added_failed: list[disnake.Role] | None = None,
    removed_failed: list[disnake.Role] | None = None,
) -> disnake.Embed:
    """Один и тот же вид — и для эфемерного подтверждения руководителю, и для лога в
    roles.log_channel_id. Цвет полосы embed'а показывает направление изменения с первого
    взгляда: зелёный — только выдали, оранжевый — только сняли, обычный — и то и другое."""
    if added and not removed:
        color = disnake.Color.green()
    elif removed and not added:
        color = disnake.Color.orange()
    else:
        color = disnake.Color.blurple()

    embed = base_embed(f"{icon_tag('users')} {title}", color=color, timestamp=True)
    embed.add_field(name=f"{icon_tag('users')} Участник", value=_member_line(member), inline=False)

    lines = [f"{icon_tag('plus')} Добавлена роль: {role.mention}" for role in added]
    lines += [f"{icon_tag('delete')} Удалена роль: {role.mention}" for role in removed]
    embed.add_field(name="Результат", value="\n".join(lines) or "—", inline=False)

    if added_failed or removed_failed:
        fail_lines = [f"{icon_tag('alert')} Не удалось выдать: {role.mention}" for role in (added_failed or [])]
        fail_lines += [f"{icon_tag('alert')} Не удалось снять: {role.mention}" for role in (removed_failed or [])]
        embed.add_field(name=f"{icon_tag('alert')} Не всё получилось", value="\n".join(fail_lines), inline=False)

    embed.add_field(name=f"{icon_tag('shield')} Автор", value=executor.mention, inline=False)
    return embed


async def _send_role_log(guild: disnake.Guild, embed: disnake.Embed) -> None:
    channel_id = config.get("roles.log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except disnake.HTTPException:
        pass


async def _apply_role_changes(
    member: disnake.Member, executor: disnake.abc.User, added: list[disnake.Role], removed: list[disnake.Role]
) -> tuple[list[disnake.Role], list[disnake.Role], list[disnake.Role], list[disnake.Role]]:
    """Каждая роль применяется отдельным запросом — если одна роль не даётся боту
    по иерархии, это не должно блокировать остальные изменения из того же выбора."""
    added_ok, added_failed = [], []
    for role in added:
        try:
            await member.add_roles(role, reason=f"/role — изменил {executor}")
            added_ok.append(role)
        except disnake.HTTPException:
            added_failed.append(role)

    removed_ok, removed_failed = [], []
    for role in removed:
        try:
            await member.remove_roles(role, reason=f"/role — изменил {executor}")
            removed_ok.append(role)
        except disnake.HTTPException:
            removed_failed.append(role)

    return added_ok, added_failed, removed_ok, removed_failed


# ---------------------------------------------------------------------------
# Один нативный мульти-select ролей Discord — сразу показывает все роли участника
# как готовые «фишки» (можно снять крестиком) и позволяет добавить любые другие
# с поиском по названию и счётчиком участников — как в стандартном редакторе ролей.
# ---------------------------------------------------------------------------

class RoleMultiSelect(disnake.ui.RoleSelect):
    def __init__(self, member: disnake.Member):
        self.member = member
        current_roles = [r for r in member.roles if r != member.guild.default_role]
        self.original_role_ids = {r.id for r in current_roles}
        super().__init__(
            placeholder="Роли участника — добавляйте и убирайте",
            min_values=0,
            max_values=MAX_ROLES_IN_SELECT,
            default_values=current_roles[:MAX_ROLES_IN_SELECT],
        )

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.defer()

        selected_ids = {role.id for role in self.values}
        added = [role for role in self.values if role.id not in self.original_role_ids]
        removed = [role for role in self.member.roles if role.id in (self.original_role_ids - selected_ids)]

        if not added and not removed:
            await inter.edit_original_message(
                embed=_status_embed("check", "Без изменений", "Набор ролей не поменялся."), view=None
            )
            return

        added_ok, added_failed, removed_ok, removed_failed = await _apply_role_changes(
            self.member, inter.author, added, removed
        )

        embed = _build_change_embed(
            "Роли обновлены", self.member, added_ok, removed_ok, inter.author,
            added_failed=added_failed, removed_failed=removed_failed,
        )
        await inter.edit_original_message(embed=embed, view=None)

        if added_ok or removed_ok:
            log_embed = _build_change_embed(
                "Изменение ролей", self.member, added_ok, removed_ok, inter.author,
                added_failed=added_failed, removed_failed=removed_failed,
            )
            await _send_role_log(inter.guild, log_embed)


class RoleMultiSelectView(disnake.ui.View):
    def __init__(self, member: disnake.Member):
        super().__init__(timeout=180)
        self.add_item(RoleMultiSelect(member))


# ---------------------------------------------------------------------------
# Слэш-команда
# ---------------------------------------------------------------------------

class Roles(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="role", description="Выдать или снять роли участнику (только для руководства)")
    async def role(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Кому изменить роли"),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return

        embed = _status_embed(
            "users", "Управление ролями", f"Участник: {member.mention}\nВыберите роли в списке ниже."
        )
        await inter.response.send_message(embed=embed, view=RoleMultiSelectView(member), ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Roles(bot))
