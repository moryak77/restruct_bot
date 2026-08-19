from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import base_embed, panel_file_kwargs
from core.config import config
from core.icons import icon_tag

ADMIN_PERMS = disnake.Permissions(manage_guild=True)


def _recruit_channel_mention() -> str | None:
    channel_id = config.get("channels.recruit_apply")
    return f"<#{channel_id}>" if channel_id else None


def _build_welcome_embed(member: disnake.Member) -> disnake.Embed:
    guild = member.guild
    family = config.get("family_name", "RESTRUCT")
    recruit_mention = _recruit_channel_mention()

    # Если у сервера есть свой баннер (настройки сервера Discord) — используем его как
    # картинку embed'а. Если баннера нет, используем ручной баннер, загруженный через /img —
    # оба варианта одновременно не поместятся, поэтому файл /img подключаем только как запасной.
    has_guild_banner = guild.banner is not None
    embed = base_embed(
        f"{icon_tag('users')} Добро пожаловать в семью {family}!",
        (
            f"{member.mention}, рады видеть тебя в **{family}** — одной из самых живых семей GTA5RP. "
            "Здесь не просто играют, а строят: свой автопарк, общую казну, контракты и команду, "
            "которая реально на связи."
        ),
        panel_key=None if has_guild_banner else "welcome",
    )
    if has_guild_banner:
        embed.set_image(url=guild.banner.with_size(1024).url)
    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)
    apply_line = f"{icon_tag('clipboard')} **Заявка в семью** — открывается тикетом"
    apply_line += f" в {recruit_mention}" if recruit_mention else ""
    apply_line += ", рассматриваем обычно в течение 24 часов\n"
    embed.add_field(
        name=f"{icon_tag('help')} Что нужно знать",
        value=(
            apply_line
            + f"{icon_tag('alert')} **Важно** — заявки принимаем только при открытом наборе, указывай "
            "в анкете только достоверные данные: это напрямую влияет на решение"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{icon_tag('users')} Нас уже",
        value=f"**{guild.member_count}** участников — и семья продолжает расти",
        inline=False,
    )
    embed.set_footer(text=f"Добро пожаловать в {family} • Удачи в игре!")
    return embed


def _build_dm_greeting_embed(member: disnake.Member) -> disnake.Embed:
    """Тот же смысл, что и публичное приветствие в канале, но короче и лично в ЛС — на
    случай, если новичок не заметит сообщение в общем канале."""
    guild = member.guild
    family = config.get("family_name", "RESTRUCT")
    recruit_mention = _recruit_channel_mention()

    embed = base_embed(
        f"{icon_tag('users')} Добро пожаловать в семью {family}!",
        f"Привет! Рады видеть тебя на сервере **{guild.name}** (семья {family}).",
    )
    if recruit_mention:
        embed.add_field(
            name=f"{icon_tag('clipboard')} Заявка в семью",
            value=f"Открывается тикетом в {recruit_mention} — рассматриваем обычно в течение 24 часов.",
            inline=False,
        )
    embed.set_footer(text=f"Увидимся на сервере, {family}!")
    return embed


def _welcome_file_kwargs(guild: disnake.Guild) -> dict:
    """Файл-вложение с ручным баннером /img нужен только тогда, когда у сервера нет своего
    баннера (иначе embed уже использует его через set_image и вложение будет лишним)."""
    if guild.banner is not None:
        return {}
    return panel_file_kwargs("welcome")


def _greeting(member: disnake.Member) -> str:
    family = config.get("family_name", "RESTRUCT")
    return f"Добро пожаловать на сервер семьи **{family}**, {member.mention}!"


class Welcome(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        if member.bot:
            return

        channel_id = config.get("channels.welcome")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            try:
                await channel.send(
                    content=_greeting(member),
                    embed=_build_welcome_embed(member),
                    **_welcome_file_kwargs(member.guild),
                )
            except disnake.HTTPException:
                pass

        try:
            await member.send(content=_greeting(member), embed=_build_dm_greeting_embed(member))
        except (disnake.Forbidden, disnake.HTTPException):
            pass  # закрытые ЛС — ожидаемо, публичное приветствие в канале всё равно ушло

    @commands.slash_command(
        name="welcome",
        description="Приветствие новых участников",
        default_member_permissions=ADMIN_PERMS,
    )
    async def welcome(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @welcome.sub_command(
        name="preview", description="Показать, как выглядит приветствие новых участников (только вам)"
    )
    async def welcome_preview(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.send_message(
            content=_greeting(inter.author),
            embed=_build_welcome_embed(inter.author),
            **_welcome_file_kwargs(inter.guild),
            ephemeral=True,
        )


def setup(bot: commands.InteractionBot):
    bot.add_cog(Welcome(bot))
