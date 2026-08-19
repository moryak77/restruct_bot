from __future__ import annotations

import disnake
from disnake.ext import commands, tasks

from cogs.shop import SHOP_EMOJI
from core.branding import base_embed, panel_file_kwargs
from core.config import config
from core.icons import icon_tag
from core.storage import (
    bank_store,
    cars_store,
    contracts_store,
    payments_store,
    redux_store,
    stats_store,
    tickets_store,
    voice_store,
    warns_store,
)

ADMIN_PERMS = disnake.Permissions(manage_guild=True)
REFRESH_SECONDS = config.get("stats.refresh_seconds", 300) or 300


def _fmt_money(amount) -> str:
    return f"{amount:,.0f} ₽".replace(",", " ")


async def _build_stats_embed(guild: disnake.Guild) -> disnake.Embed:
    members = guild.members
    total = guild.member_count or len(members)
    bots = sum(1 for m in members if m.bot)
    humans = max(total - bots, 0)
    online = sum(
        1 for m in members
        if not m.bot and m.status is not None and m.status is not disnake.Status.offline
    )

    embed = base_embed(
        f"{icon_tag('clipboard')} Статистика семьи {config.get('family_name', 'RESTRUCT')}",
        panel_key="stats",
        timestamp=True,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name=f"{icon_tag('users')} Участников", value=f"**{total}**", inline=True)
    embed.add_field(name="Людей / ботов", value=f"**{humans}** / {bots}", inline=True)
    embed.add_field(name="Онлайн сейчас", value=f"**{online}**", inline=True)

    embed.add_field(
        name="Буст-статус",
        value=f"Уровень {guild.premium_tier} · {guild.premium_subscription_count or 0} бустов",
        inline=True,
    )
    embed.add_field(
        name="Каналы",
        value=f"{len(guild.text_channels)} текст. · {len(guild.voice_channels)} голос. · {len(guild.categories)} катег.",
        inline=True,
    )
    embed.add_field(name="Ролей", value=str(max(len(guild.roles) - 1, 0)), inline=True)

    try:
        invites = await guild.invites()
        embed.add_field(name=f"{icon_tag('send')} Активных инвайтов", value=str(len(invites)), inline=True)
    except disnake.Forbidden:
        pass  # у бота нет права "Управление сервером" — пропускаем без ошибки

    embed.add_field(name="Сервер основан", value=disnake.utils.format_dt(guild.created_at, style="D"), inline=True)
    embed.add_field(name="ID сервера", value=f"`{guild.id}`", inline=True)

    cars_count = len(cars_store.load()["cars"])
    tickets_data = tickets_store.load()["open"]
    open_tickets = len(tickets_data)
    pending_recruit = sum(
        1 for info in tickets_data.values() if info.get("type") == "recruit" and info.get("decision") is None
    )
    active_warns = sum(1 for w in warns_store.load()["warns"] if w.get("active"))
    bank_balance = bank_store.load().get("balance", 0)
    contracts_count = len(contracts_store.load()["contracts"])
    active_voice = len(voice_store.load()["channels"])
    shop_paid = sum(1 for o in payments_store.load()["orders"] if o.get("status") == "paid")
    redux_categories = redux_store.load().get("categories", {})
    redux_total = sum(len(v) for v in redux_categories.values())

    embed.add_field(
        name=f"{icon_tag('clipboard')} Активность семьи",
        value=(
            f"{icon_tag('car')} Автопарк: **{cars_count}**\n"
            f"{icon_tag('ticket')} Открытых тикетов: **{open_tickets}** (из них заявок в семью: **{pending_recruit}**)\n"
            f"{icon_tag('alert')} Активных предупреждений: **{active_warns}**\n"
            f"{icon_tag('bank')} Баланс банка: **{_fmt_money(bank_balance)}**\n"
            f"Активных контрактов: **{contracts_count}**\n"
            f"Приватных голосовых сейчас: **{active_voice}**\n"
            f"{SHOP_EMOJI['cart']} Оплаченных заказов в магазине: **{shop_paid}**\n"
            f"{icon_tag('package')} Пунктов в каталоге модов: **{redux_total}**"
        ),
        inline=False,
    )

    embed.set_footer(text=f"Обновляется каждые {max(REFRESH_SECONDS // 60, 1)} мин · Семья {config.get('family_name', 'RESTRUCT')}")
    return embed


async def _refresh_stats_panel(guild: disnake.Guild) -> None:
    data = stats_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=await _build_stats_embed(guild))
    except disnake.HTTPException:
        pass


class Stats(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def refresh_loop(self):
        data = stats_store.load()
        guild_id = data.get("guild_id")
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        await _refresh_stats_panel(guild)

    @refresh_loop.before_loop
    async def _before_refresh_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="stats",
        description="Статистика сервера",
        default_member_permissions=ADMIN_PERMS,
    )
    async def stats(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @stats.sub_command(name="show", description="Опубликовать самообновляющуюся панель статистики сервера (только для персонала)")
    async def stats_show(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        embed = await _build_stats_embed(inter.guild)
        message = await inter.channel.send(embed=embed, **panel_file_kwargs("stats"))
        await inter.followup.send("✅ Панель статистики опубликована в этом канале.", ephemeral=True)

        data = stats_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        data["guild_id"] = inter.guild_id
        stats_store.save(data)

    @stats.sub_command(name="refresh", description="Обновить панель статистики прямо сейчас (только для персонала)")
    async def stats_refresh(self, inter: disnake.ApplicationCommandInteraction):
        data = stats_store.load()
        if not data.get("panel_channel_id"):
            await inter.response.send_message("❌ Панель статистики ещё не опубликована — сначала вызовите `/stats show`.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        await _refresh_stats_panel(inter.guild)
        await inter.followup.send("✅ Обновлено.", ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Stats(bot))
