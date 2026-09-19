from __future__ import annotations

import datetime as dt
import logging

import disnake
from disnake.ext import commands, tasks

from core.config import config
from core.http import get_session

log = logging.getLogger("restruct-bot")

PROFILE_SYNC_MINUTES = 5
MESSAGE_FLUSH_SECONDS = 10
VOICE_CHECKPOINT_SECONDS = 30
_BATCH_SIZE = 200

# disnake PublicUserFlags attr -> формат бейджей, который уже понимает сайт (lib/discord-badges.ts)
_BADGE_FLAG_MAP = {
    "staff": "Staff",
    "partner": "Partner",
    "hypesquad": "Hypesquad",
    "hypesquad_bravery": "HypeSquadOnlineHouse1",
    "hypesquad_brilliance": "HypeSquadOnlineHouse2",
    "hypesquad_balance": "HypeSquadOnlineHouse3",
    "bug_hunter": "BugHunterLevel1",
    "bug_hunter_level_2": "BugHunterLevel2",
    "early_supporter": "PremiumEarlySupporter",
    "early_verified_bot_developer": "VerifiedDeveloper",
    "discord_certified_moderator": "CertifiedModerator",
    "active_developer": "ActiveDeveloper",
}


def _site_base_url() -> str:
    return (config.get("verification.site_base_url") or "http://127.0.0.1:3000").rstrip("/")


def _api_secret() -> str:
    return config.get("verification.api_secret") or ""


def _role_color_hex(role: disnake.Role) -> str:
    value = role.color.value
    return f"#{value:06x}" if value else "#99aab5"


def _member_badges(member: disnake.Member) -> list[str]:
    try:
        flags = member.public_flags
    except AttributeError:
        return []
    badges = []
    for attr, name in _BADGE_FLAG_MAP.items():
        if getattr(flags, attr, False):
            badges.append(name)
    return badges


def _build_profile_event(member: disnake.Member) -> dict:
    roles = [
        {"id": str(r.id), "name": r.name, "color": _role_color_hex(r)}
        for r in member.roles
        if not r.is_default()
    ]
    return {
        "type": "profile",
        "discordId": str(member.id),
        "username": member.name,
        "avatar": member.display_avatar.with_size(256).url,
        "nickname": member.nick,
        "roles": roles,
        "badges": _member_badges(member),
        "boosting": member.premium_since is not None,
        "joinedAt": member.joined_at.isoformat() if member.joined_at else None,
    }


class SiteSync(commands.Cog):
    """Синхронизирует профиль/роли, войс-активность и счётчик сообщений привязанных
    аккаунтов с сайтом — единственный источник этих данных теперь этот бот (реальный
    Discord-сервер семьи), не отдельный вспомогательный бот на стороне сайта."""

    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._voice_joined_at: dict[int, dt.datetime] = {}
        self._voice_checkpointed: set[int] = set()
        self._message_deltas: dict[int, int] = {}
        self._linked_ids: set[str] = set()
        self._started = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True
        if not _api_secret():
            log.warning(
                "verification.api_secret пуст — синхронизация с сайтом (роли/войс/сообщения) отключена."
            )
            return
        # Кто уже сидит в войсе на момент старта бота — иначе их сессия потерялась бы.
        now = dt.datetime.now(dt.timezone.utc)
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if not member.bot:
                        self._voice_joined_at.setdefault(member.id, now)
        self.profile_sync_loop.start()
        self.message_flush_loop.start()
        self.voice_checkpoint_loop.start()

    def cog_unload(self) -> None:
        self.profile_sync_loop.cancel()
        self.message_flush_loop.cancel()
        self.voice_checkpoint_loop.cancel()

    async def _fetch_linked_ids(self) -> set[str]:
        session = get_session()
        try:
            async with session.get(
                f"{_site_base_url()}/api/discord/linked-ids",
                headers={"X-Api-Key": _api_secret()},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    log.warning("Не удалось получить список привязанных аккаунтов: %s", resp.status)
                    return self._linked_ids
                data = await resp.json()
                return set(data.get("ids", []))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить список привязанных аккаунтов: %s", e)
            return self._linked_ids

    async def _post_events(self, events: list[dict]) -> None:
        if not events or not _api_secret():
            return
        session = get_session()
        # Батчами по BATCH_SIZE — сервер на 4000+ участников не влезет в один запрос.
        for i in range(0, len(events), _BATCH_SIZE):
            chunk = events[i : i + _BATCH_SIZE]
            try:
                async with session.post(
                    f"{_site_base_url()}/api/discord/sync",
                    headers={"X-Api-Key": _api_secret()},
                    json={"events": chunk},
                    timeout=20,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Синк с сайтом вернул %s: %s", resp.status, body[:300])
            except Exception as e:  # noqa: BLE001 — синк не должен ронять бота
                log.warning("Не удалось отправить синк на сайт: %s", e)

    @tasks.loop(minutes=PROFILE_SYNC_MINUTES)
    async def profile_sync_loop(self) -> None:
        # Сервер большой (тысячи участников), а привязанных к сайту — единицы/десятки.
        # Сначала спрашиваем у сайта, кого вообще нужно синкать, вместо того чтобы
        # гонять профили всех подряд.
        self._linked_ids = await self._fetch_linked_ids()
        if not self._linked_ids:
            return

        events = []
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot or str(member.id) not in self._linked_ids:
                    continue
                events.append(_build_profile_event(member))
        await self._post_events(events)

    @profile_sync_loop.before_loop
    async def _before_profile_sync(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=MESSAGE_FLUSH_SECONDS)
    async def message_flush_loop(self) -> None:
        if not self._message_deltas:
            return
        events = [
            {"type": "message_delta", "discordId": str(uid), "delta": delta}
            for uid, delta in self._message_deltas.items()
            if delta > 0
        ]
        self._message_deltas.clear()
        await self._post_events(events)

    @tasks.loop(seconds=VOICE_CHECKPOINT_SECONDS)
    async def voice_checkpoint_loop(self) -> None:
        # Раньше время в войсе уходило на сайт только при выходе из канала — статистика
        # отставала на всю длину сессии. Теперь каждые N секунд отправляем накопленный кусок;
        # сайт склеивает куски, начинающиеся там, где закончился предыдущий, в одну сессию.
        if not self._voice_joined_at:
            return
        now = dt.datetime.now(dt.timezone.utc)
        events = []
        for uid, joined_at in list(self._voice_joined_at.items()):
            duration = int((now - joined_at).total_seconds())
            if duration < 1:
                continue
            events.append(
                {
                    "type": "voice_session",
                    "discordId": str(uid),
                    "joinedAt": joined_at.isoformat(),
                    "leftAt": now.isoformat(),
                    "durationSeconds": duration,
                }
            )
            self._voice_joined_at[uid] = now
            self._voice_checkpointed.add(uid)
        await self._post_events(events)

    @commands.Cog.listener()
    async def on_member_update(self, before: disnake.Member, after: disnake.Member):
        if after.bot:
            return
        if before.roles == after.roles and before.nick == after.nick:
            return
        await self._post_events([_build_profile_event(after)])

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if message.author.bot or message.guild is None:
            return
        self._message_deltas[message.author.id] = self._message_deltas.get(message.author.id, 0) + 1

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState
    ):
        if member.bot:
            return

        if before.channel is None and after.channel is not None:
            self._voice_joined_at[member.id] = dt.datetime.now(dt.timezone.utc)
            self._voice_checkpointed.discard(member.id)
            return

        if before.channel is not None and after.channel is None:
            joined_at = self._voice_joined_at.pop(member.id, None)
            if joined_at is None:
                return
            left_at = dt.datetime.now(dt.timezone.utc)
            duration = int((left_at - joined_at).total_seconds())
            was_checkpointed = member.id in self._voice_checkpointed
            self._voice_checkpointed.discard(member.id)
            if duration < 30 and not was_checkpointed:
                return  # слишком короткая сессия — не считаем (защита от дребезга)
            await self._post_events(
                [
                    {
                        "type": "voice_session",
                        "discordId": str(member.id),
                        "joinedAt": joined_at.isoformat(),
                        "leftAt": left_at.isoformat(),
                        "durationSeconds": duration,
                    }
                ]
            )


def setup(bot: commands.InteractionBot):
    bot.add_cog(SiteSync(bot))
