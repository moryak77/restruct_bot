from __future__ import annotations

import datetime as dt

import disnake
from disnake.ext import commands, tasks

from core.branding import base_embed
from core.config import config, log_channel_id
from core.icons import icon_tag
from core.storage import warns_store

MOD_PERMS = disnake.Permissions(moderate_members=True)
WARN_EXPIRY_CHECK_MINUTES = 30  # как часто проверяем предупреждения на истечение недельного срока

# Роли уровня предупреждений — визуальный бейдж «Warn 1/2/3» на участнике. Создаются лениво
# при первой необходимости и больше никогда не удаляются с сервера ботом (id хранится
# в data/warns.json навсегда и переиспользуется), даже если сейчас у уровня нет ни одного
# участника — только назначение/снятие роли конкретному участнику меняется.
LEVEL_ROLE_NAMES = {1: "⚠️ Warn 1", 2: "🔶 Warn 2", 3: "🛑 Warn 3"}
LEVEL_ROLE_COLORS = {1: disnake.Color.gold(), 2: disnake.Color.orange(), 3: disnake.Color.red()}


def _issue_warn(guild_id: int, user_id: int, moderator_id: int, reason: str) -> dict:
    data = warns_store.load()
    warn = {
        "id": data["next_id"],
        "guild_id": guild_id,
        "user_id": user_id,
        "moderator_id": moderator_id,
        "reason": reason,
        "date": dt.datetime.now(dt.timezone.utc).isoformat(),
        "active": True,
    }
    data["warns"].append(warn)
    data["next_id"] += 1
    warns_store.save(data)
    return warn


def _active_warns(guild_id: int, user_id: int) -> list[dict]:
    data = warns_store.load()
    return [w for w in data["warns"] if w["guild_id"] == guild_id and w["user_id"] == user_id and w["active"]]


def _lift_warn(warn_id: int) -> dict | None:
    data = warns_store.load()
    for warn in data["warns"]:
        if warn["id"] == warn_id and warn["active"]:
            warn["active"] = False
            warns_store.save(data)
            return warn
    return None


async def _get_or_create_level_role(guild: disnake.Guild, level: int) -> disnake.Role | None:
    data = warns_store.load()
    role_ids: dict = data.setdefault("level_role_ids", {})
    role_id = role_ids.get(str(level))
    role = guild.get_role(role_id) if role_id else None
    if role is not None:
        return role

    try:
        role = await guild.create_role(
            name=LEVEL_ROLE_NAMES[level],
            color=LEVEL_ROLE_COLORS[level],
            hoist=False,
            mentionable=False,
            reason="Автосоздание роли уровня предупреждений (создаётся один раз и больше не удаляется)",
        )
    except disnake.HTTPException:
        return None

    role_ids[str(level)] = role.id
    data["level_role_ids"] = role_ids
    warns_store.save(data)
    return role


async def _sync_warn_level_role(guild: disnake.Guild, member: disnake.Member, active_count: int) -> None:
    """Текущий уровень (1/2/3) отображается ровно одной ролью — при выдаче/снятии
    предупреждения снимаем роли других уровней и выставляем нужную."""
    target_level = min(active_count, 3)
    for level in (1, 2, 3):
        role = await _get_or_create_level_role(guild, level)
        if role is None:
            continue
        has_role = role in member.roles
        should_have = level == target_level
        try:
            if should_have and not has_role:
                await member.add_roles(role, reason="Уровень предупреждений")
            elif not should_have and has_role:
                await member.remove_roles(role, reason="Уровень предупреждений изменился")
        except disnake.HTTPException:
            pass


async def _auto_ban_for_warns(guild: disnake.Guild, target: disnake.Member) -> tuple[bool, str | None]:
    """При достижении warns.threshold активных предупреждений цель автоматически уходит
    в бан на warns.auto_ban_days дней. Импорт cogs.moderation — локальный, внутри функции,
    а не на уровне модуля: moderation.py уже импортирует warns.py при загрузке, импорт в
    обратную сторону на уровне модуля создал бы цикл (сработает нормально здесь, так как
    к моменту вызова оба модуля уже полностью инициализированы)."""
    from cogs.moderation import _do_ban

    threshold = config.get("warns.threshold", 3)
    days = config.get("warns.auto_ban_days", 3)
    delta = dt.timedelta(days=days)
    reason = f"Автобан — достигнут порог предупреждений ({threshold})"
    ok, error, _dm_ok = await _do_ban(guild, target, delta, reason, guild.me)
    return ok, error


async def issue_warn(
    guild: disnake.Guild, target: disnake.Member, moderator: disnake.abc.User, reason: str
) -> tuple[dict, int, str, bool]:
    """ЛС цели + запись + лог + роль уровня предупреждений + автобан по достижении
    warns.threshold — общая логика для /warn и заявок на наказание из cogs.moderation,
    чтобы оба пути гарантированно вели себя одинаково."""
    warn = _issue_warn(guild.id, target.id, moderator.id, reason)

    dm_embed = base_embed(
        f"{icon_tag('alert')} Вам выдано предупреждение",
        f"В семье **{guild.name}** вам было выдано предупреждение.",
        color=disnake.Color.orange(),
    )
    dm_embed.add_field(name="Причина", value=reason, inline=False)
    dm_embed.add_field(name="Модератор", value=moderator.mention, inline=False)
    try:
        await target.send(embed=dm_embed)
        dm_status = "Пользователь уведомлён в ЛС."
    except disnake.Forbidden:
        dm_status = "Не удалось отправить уведомление в ЛС (закрыты личные сообщения)."

    active_count = len(_active_warns(guild.id, target.id))
    threshold = config.get("warns.threshold", 3)
    auto_banned = False
    ban_error: str | None = None
    if active_count == threshold:
        auto_banned, ban_error = await _auto_ban_for_warns(guild, target)
        if not auto_banned:
            # Не получилось забанить (например, роль бота ниже цели) — уровень предупреждений
            # всё равно должен отражать реальность, раз бана не случилось.
            await _sync_warn_level_role(guild, target, active_count)
    else:
        await _sync_warn_level_role(guild, target, active_count)

    channel_id = log_channel_id("warns")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            log_embed = base_embed(f"{icon_tag('alert')} Выдано предупреждение", color=disnake.Color.orange(), timestamp=True)
            log_embed.add_field(name="Участник", value=target.mention, inline=True)
            log_embed.add_field(name="Модератор", value=moderator.mention, inline=True)
            log_embed.add_field(name="ID предупреждения", value=f"#{warn['id']}", inline=True)
            log_embed.add_field(name="Причина", value=reason, inline=False)
            await channel.send(embed=log_embed)

            if active_count == threshold:
                escalation_role_ids = config.get("warns.escalation_role_ids", []) or []
                mentions = " ".join(f"<@&{rid}>" for rid in escalation_role_ids if rid)
                ban_days = config.get("warns.auto_ban_days", 3)
                if auto_banned:
                    esc_description = (
                        f"У участника {target.mention} накопилось **{active_count}** активных предупреждений — "
                        f"выдан автоматический бан на **{ban_days} дн.**"
                    )
                else:
                    esc_description = (
                        f"У участника {target.mention} накопилось **{active_count}** активных предупреждений — "
                        f"автобан не удался ({ban_error}), нужно решение вручную."
                    )
                esc_embed = base_embed(
                    f"{icon_tag('alert')} Порог предупреждений достигнут",
                    esc_description,
                    color=disnake.Color.red(),
                    timestamp=True,
                )
                await channel.send(
                    content=mentions or None,
                    embed=esc_embed,
                    allowed_mentions=disnake.AllowedMentions(roles=True),
                )

    return warn, active_count, dm_status, auto_banned


async def lift_warn(
    guild: disnake.Guild, target: disnake.abc.User, moderator: disnake.abc.User, warn_id: int, *, automatic: bool = False
) -> dict | None:
    """Снимает предупреждение, уведомляет цель в ЛС и пишет в лог — общая логика для
    /unwarn, заявок на снятие наказания из cogs.moderation и автоматического истечения
    срока предупреждения (automatic=True — неделя по умолчанию, warns.auto_expire_days)."""
    warn = _lift_warn(warn_id)
    if warn is None:
        return None

    dm_embed = base_embed(
        f"{icon_tag('check')} С вас снято предупреждение",
        f"В семье **{guild.name}** с вас было снято предупреждение.",
        color=disnake.Color.green(),
    )
    dm_embed.add_field(name="Причина (была)", value=warn["reason"], inline=False)
    dm_embed.add_field(
        name="Снял",
        value="Автоматически (истёк срок предупреждения)" if automatic else moderator.mention,
        inline=False,
    )
    try:
        await target.send(embed=dm_embed)
    except (disnake.Forbidden, disnake.HTTPException):
        pass

    member = guild.get_member(target.id)
    if member is not None:
        await _sync_warn_level_role(guild, member, len(_active_warns(guild.id, target.id)))

    channel_id = log_channel_id("warns")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            log_embed = base_embed(f"{icon_tag('check')} Предупреждение снято", color=disnake.Color.green(), timestamp=True)
            log_embed.add_field(name="Участник", value=target.mention, inline=True)
            log_embed.add_field(
                name="Снял", value="🤖 Автоматически (истёк срок)" if automatic else moderator.mention, inline=True
            )
            log_embed.add_field(name="ID предупреждения", value=f"#{warn_id}", inline=True)
            log_embed.add_field(name="Причина (была)", value=warn["reason"], inline=False)
            await channel.send(embed=log_embed)

    return warn


async def _finalize_warn(inter: disnake.Interaction, target: disnake.Member, reason: str):
    warn, active_count, dm_status, auto_banned = await issue_warn(inter.guild, target, inter.author, reason)
    text = (
        f"✅ Предупреждение **#{warn['id']}** выдано {target.mention}.\n"
        f"Активных предупреждений: **{active_count}**. {dm_status}"
    )
    if auto_banned:
        ban_days = config.get("warns.auto_ban_days", 3)
        text += f"\n🚨 Достигнут порог предупреждений — {target.mention} автоматически забанен(а) на {ban_days} дн."
    await inter.response.send_message(text, ephemeral=True)


class WarnReasonModal(disnake.ui.Modal):
    def __init__(self, target: disnake.Member):
        self.target = target
        components = [
            disnake.ui.TextInput(
                label="Причина предупреждения",
                custom_id="reason",
                style=disnake.TextInputStyle.paragraph,
                max_length=300,
            ),
        ]
        super().__init__(title=f"Предупреждение: {target.display_name}"[:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await _finalize_warn(inter, self.target, inter.text_values["reason"].strip())


class UnwarnSelect(disnake.ui.StringSelect):
    def __init__(self, target: disnake.abc.User, warns: list[dict]):
        self.target = target
        options = [
            disnake.SelectOption(
                label=f"#{w['id']} — {w['reason'][:80]}",
                value=str(w["id"]),
            )
            for w in warns[:25]
        ]
        super().__init__(placeholder="Выберите предупреждение для снятия", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        warn_id = int(self.values[0])
        warn = await lift_warn(inter.guild, self.target, inter.author, warn_id)
        if warn is None:
            await inter.response.edit_message(content="❌ Предупреждение не найдено или уже снято.", view=None)
            return
        await inter.response.edit_message(
            content=f"✅ Предупреждение **#{warn_id}** для {self.target.mention} снято.",
            view=None,
        )


class UnwarnView(disnake.ui.View):
    def __init__(self, target: disnake.abc.User, warns: list[dict]):
        super().__init__(timeout=120)
        self.add_item(UnwarnSelect(target, warns))


class Warns(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.check_expired_warns.start()

    def cog_unload(self):
        self.check_expired_warns.cancel()

    @tasks.loop(minutes=WARN_EXPIRY_CHECK_MINUTES)
    async def check_expired_warns(self):
        """Каждое предупреждение снимается через warns.auto_expire_days (по умолчанию
        неделю) после выдачи — независимо для каждого, а не только для самого старого."""
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return

        expire_days = config.get("warns.auto_expire_days", 7)
        now = dt.datetime.now(dt.timezone.utc)
        data = warns_store.load()
        due = [
            w for w in data["warns"]
            if w["active"] and w["guild_id"] == guild.id
            and now - dt.datetime.fromisoformat(w["date"]) >= dt.timedelta(days=expire_days)
        ]
        for warn in due:
            try:
                target = await self.bot.fetch_user(warn["user_id"])
            except disnake.HTTPException:
                continue
            await lift_warn(guild, target, guild.me, warn["id"], automatic=True)

    @check_expired_warns.before_loop
    async def _before_check_expired_warns(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="warn",
        description="Выдать пользователю предупреждение",
        default_member_permissions=MOD_PERMS,
    )
    async def warn(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Кому выдать предупреждение"),
        reason: str = commands.Param(description="Причина предупреждения", max_length=300),
    ):
        await _finalize_warn(inter, member, reason)

    @commands.user_command(name="Выдать предупреждение", default_member_permissions=MOD_PERMS)
    async def warn_context(self, inter: disnake.UserCommandInteraction):
        if not isinstance(inter.target, disnake.Member):
            await inter.response.send_message("❌ Этот пользователь не является участником сервера.", ephemeral=True)
            return
        await inter.response.send_modal(WarnReasonModal(inter.target))

    @commands.slash_command(
        name="unwarn",
        description="Снять выбранное предупреждение пользователя",
        default_member_permissions=MOD_PERMS,
    )
    async def unwarn(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="У кого снять предупреждение"),
    ):
        warns = _active_warns(inter.guild_id, member.id)
        if not warns:
            await inter.response.send_message(f"У {member.mention} нет активных предупреждений.", ephemeral=True)
            return
        await inter.response.send_message(
            f"Выберите предупреждение, которое нужно снять у {member.mention}:",
            view=UnwarnView(member, warns),
            ephemeral=True,
        )

    @commands.slash_command(name="warns", description="Показать активные предупреждения пользователя")
    async def warns(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Чьи предупреждения показать"),
    ):
        warns = _active_warns(inter.guild_id, member.id)
        embed = base_embed(f"{icon_tag('alert')} Предупреждения: {member.display_name}", color=disnake.Color.orange())
        if not warns:
            embed.description = "Активных предупреждений нет."
        else:
            for w in warns:
                date = dt.datetime.fromisoformat(w["date"]).strftime("%d.%m.%Y %H:%M")
                embed.add_field(
                    name=f"#{w['id']} • {date}",
                    value=f"Причина: {w['reason']}\nМодератор: <@{w['moderator_id']}>",
                    inline=False,
                )
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Warns(bot))
