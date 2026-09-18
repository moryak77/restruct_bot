from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict, deque

import disnake
from disnake.ext import commands

from core.branding import base_embed
from core.config import config, log_channel_id
from core.icons import icon_tag

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE)


def _cfg(key: str, default):
    return config.get(f"automod.{key}", default)


class AutoMod(commands.Cog):
    """Простая защита сервера: спам-рейт, повторяющиеся сообщения, посторонние ссылки
    и приглашения на другие сервера, массовые упоминания. Нарушение = удаление сообщения +
    предупреждение через уже существующую систему варнов (cogs.warns) — так это сразу
    учитывается в общем пороге предупреждений/автобане, а не живёт отдельной системой."""

    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        # user_id -> deque[timestamp] последних сообщений (для рейт-спама)
        self._recent_messages: dict[int, deque[dt.datetime]] = defaultdict(deque)
        # user_id -> deque[(content, timestamp)] последних сообщений (для дублей)
        self._recent_content: dict[int, deque[tuple[str, dt.datetime]]] = defaultdict(deque)

    def _is_exempt(self, member: disnake.Member) -> bool:
        if member.guild_permissions.manage_messages:
            return True
        exempt_ids = set(_cfg("exempt_role_ids", []) or [])
        return any(r.id in exempt_ids for r in member.roles)

    async def _log_violation(self, message: disnake.Message, reason: str) -> None:
        channel_id = log_channel_id("automod")
        if not channel_id:
            return
        channel = message.guild.get_channel(channel_id)
        if channel is None:
            return
        embed = base_embed(
            f"{icon_tag('shield')} Автомодерация",
            f"{message.author.mention} в {message.channel.mention}",
            color=disnake.Color.red(),
            timestamp=True,
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        if message.content:
            embed.add_field(name="Сообщение", value=message.content[:500], inline=False)
        try:
            await channel.send(embed=embed)
        except disnake.HTTPException:
            pass

    async def _punish(self, message: disnake.Message, reason: str) -> None:
        try:
            await message.delete()
        except disnake.HTTPException:
            pass

        await self._log_violation(message, reason)

        if not _cfg("warn_on_violation", True):
            return

        from cogs.warns import issue_warn  # локальный импорт — избегаем цикла модулей при старте

        member = message.author
        if not isinstance(member, disnake.Member):
            return
        try:
            await issue_warn(message.guild, member, message.guild.me, f"Автомодерация: {reason}")
        except Exception:  # noqa: BLE001 — автомод не должен падать из-за сбоя в выдаче варна
            pass

    def _check_link(self, content: str, member: disnake.Member) -> str | None:
        if _INVITE_RE.search(content) and _cfg("invites.block", True):
            return "приглашение на другой Discord-сервер"

        if not _cfg("links.block", True):
            return None
        urls = _URL_RE.findall(content)
        if not urls:
            return None
        allowed = [d.lower() for d in (_cfg("links.allowed_domains", []) or [])]
        for url in urls:
            if not any(domain in url.lower() for domain in allowed):
                return "ссылка на непроверенный сайт"
        return None

    def _check_mentions(self, message: disnake.Message) -> str | None:
        max_mentions = _cfg("mentions.max_mentions", 5)
        total = len(message.mentions) + len(message.role_mentions)
        if total > max_mentions:
            return f"массовые упоминания ({total})"
        return None

    def _check_spam_rate(self, message: disnake.Message) -> str | None:
        limit = _cfg("spam.message_count", 5)
        window = _cfg("spam.interval_seconds", 6)
        now = dt.datetime.now(dt.timezone.utc)
        history = self._recent_messages[message.author.id]
        history.append(now)
        while history and (now - history[0]).total_seconds() > window:
            history.popleft()
        if len(history) > limit:
            return f"слишком много сообщений подряд ({len(history)} за {window} сек)"
        return None

    def _check_duplicates(self, message: disnake.Message) -> str | None:
        if not message.content:
            return None
        repeat_count = _cfg("duplicate.repeat_count", 3)
        window = _cfg("duplicate.window_seconds", 20)
        now = dt.datetime.now(dt.timezone.utc)
        history = self._recent_content[message.author.id]
        history.append((message.content, now))
        while history and (now - history[0][1]).total_seconds() > window:
            history.popleft()
        same = sum(1 for content, _ in history if content == message.content)
        if same >= repeat_count:
            return f"повторяющееся сообщение ({same} раз подряд)"
        return None

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot or message.guild is None:
            return
        if not _cfg("enabled", True):
            return
        if not isinstance(message.author, disnake.Member) or self._is_exempt(message.author):
            return

        reason = (
            self._check_spam_rate(message)
            or self._check_duplicates(message)
            or self._check_mentions(message)
            or self._check_link(message.content, message.author)
        )
        if reason:
            await self._punish(message, reason)


def setup(bot: commands.InteractionBot):
    bot.add_cog(AutoMod(bot))
