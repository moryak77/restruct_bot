from __future__ import annotations

import datetime as dt
import logging
import secrets
import string

import disnake
from aiohttp import web
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config, log_channel_id
from core.icons import icon, icon_tag
from core.storage import verify_store

log = logging.getLogger("restruct-bot")

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

_LOG_COLORS = {
    "info": disnake.Color(0x5865F2),
    "success": disnake.Color(0x2ECC71),
    "warning": disnake.Color(0xC9A44C),
    "danger": disnake.Color(0xC81F37),
}

# Без 0/O/1/I/L — на глаз не спутать при переписывании кода с телефона/другого монитора.
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OIL")
_CODE_LENGTH = 6


def _generate_code(existing: dict) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if code not in existing:
            return code


def _ttl_minutes() -> int:
    return int(config.get("verification.code_ttl_minutes", 15) or 15)


def _build_panel_embed() -> disnake.Embed:
    ttl = _ttl_minutes()
    return base_embed(
        f"{icon_tag('link')} Привязка аккаунта сайта",
        (
            "Нажми кнопку ниже — бот выдаст одноразовый код. Введи его на сайте RESTRUCT "
            "в личном кабинете, чтобы привязать аккаунт сайта к своему Discord.\n\n"
            f"{icon_tag('alert')} Код действует **{ttl} минут** и одноразовый — при повторном "
            "нажатии кнопки старый код аннулируется и выдаётся новый."
        ),
        panel_key="verify",
    )


def _snapshot_member(member: disnake.Member, channel_id: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "user_id": member.id,
        "guild_id": member.guild.id,
        "channel_id": channel_id,
        "username": member.name,
        "display_avatar_url": member.display_avatar.with_size(256).url,
        "nickname": member.nick,
        "guild_joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=_ttl_minutes())).isoformat(),
        "used": False,
    }


def _prune_expired(data: dict) -> bool:
    """Убирает протухшие и уже использованные коды. Возвращает True, если что-то удалили."""
    now = dt.datetime.now(dt.timezone.utc)
    before = len(data["codes"])
    data["codes"] = {
        code: entry
        for code, entry in data["codes"].items()
        if not entry["used"] and dt.datetime.fromisoformat(entry["expires_at"]) > now
    }
    return len(data["codes"]) < before


class VerifyPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(
        label="Получить код",
        style=disnake.ButtonStyle.primary,
        emoji=icon("key"),
        custom_id="verify_get_code",
    )
    async def get_code(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        data = verify_store.load()
        _prune_expired(data)
        # Одному участнику — один активный код: старые коды того же пользователя аннулируем.
        data["codes"] = {
            code: entry for code, entry in data["codes"].items() if entry["user_id"] != inter.author.id
        }
        code = _generate_code(data["codes"])
        data["codes"][code] = _snapshot_member(inter.author, inter.channel.id)
        verify_store.save(data)

        embed = base_embed(
            f"{icon_tag('key')} Твой код подтверждения",
            (
                f"## `{code}`\n\n"
                f"Введи этот код на сайте RESTRUCT в личном кабинете (раздел привязки Discord).\n"
                f"{icon_tag('alert')} Код действует **{_ttl_minutes()} минут** и работает один раз."
            ),
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


class VerifyAPI:
    """Небольшой HTTP-сервер внутри процесса бота — единственный мост, которым сайт
    обменивает код, показанный в Discord, на снимок участника (id/ник/аватар/дата входа).
    Работает только по секретному заголовку из config.verification.api_secret — без него
    сервер вообще не поднимается, чтобы случайно не оставить открытую ручку."""

    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.runner: web.AppRunner | None = None

    async def _finish_verification(self, entry: dict) -> None:
        """После успешной привязки на сайте: выдаёт роль подтверждённого участника,
        уведомляет его в канале верификации и скрывает от него этот канал — он ему
        больше не нужен. Best-effort на каждом шаге — сбой здесь не должен ломать ответ
        сайту, аккаунт уже привязан к этому моменту."""
        guild = self.bot.get_guild(entry["guild_id"])
        if guild is None:
            return

        member = guild.get_member(entry["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(entry["user_id"])
            except disnake.HTTPException:
                return

        role_id = config.get("verification.verified_role_id")
        if role_id:
            role = guild.get_role(role_id)
            if role is None:
                log.warning(
                    "verification.verified_role_id=%s не найдена на сервере %s", role_id, guild.id
                )
            else:
                try:
                    await member.add_roles(role, reason="Верификация аккаунта сайта")
                except disnake.Forbidden:
                    log.warning(
                        "Не удалось выдать роль верификации участнику %s — роль бота ниже в иерархии.",
                        member,
                    )

        channel = guild.get_channel(entry.get("channel_id") or 0)
        if channel is None:
            return

        try:
            await channel.send(
                content=member.mention,
                embed=base_embed(
                    f"{icon_tag('check')} Аккаунт привязан",
                    "Твой аккаунт сайта успешно привязан к Discord. Этот канал тебе больше не понадобится.",
                ),
            )
        except disnake.HTTPException:
            pass

        try:
            await channel.set_permissions(
                member, view_channel=False, reason="Верификация завершена — канал больше не нужен"
            )
        except disnake.Forbidden:
            log.warning(
                "Не удалось скрыть канал верификации от %s — не хватает прав у бота.", member
            )

    async def start(self) -> None:
        secret = config.get("verification.api_secret") or ""
        if not secret:
            log.warning(
                "verification.api_secret не задан в config.json — HTTP API для привязки "
                "аккаунтов сайта не запущен (кнопка в Discord работать не будет)."
            )
            return

        host = config.get("verification.api_host", "127.0.0.1") or "127.0.0.1"
        port = int(config.get("verification.api_port", 8787) or 8787)

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/verify-code", self._handle_verify)
        app.router.add_post("/create-recruit-ticket", self._handle_create_recruit_ticket)
        app.router.add_post("/ticket-log", self._handle_ticket_log)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        log.info("Verification API запущен на http://%s:%s", host, port)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_verify(self, request: web.Request) -> web.Response:
        secret = config.get("verification.api_secret") or ""
        if request.headers.get("X-Api-Key") != secret:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid_body"}, status=400)

        code = str(payload.get("code", "")).strip().upper()
        if not code:
            return web.json_response({"error": "missing_code"}, status=400)

        data = verify_store.load()
        _prune_expired(data)
        entry = data["codes"].get(code)
        if entry is None:
            verify_store.save(data)
            return web.json_response({"error": "invalid_code"}, status=404)

        entry["used"] = True
        verify_store.save(data)

        await self._finish_verification(entry)

        return web.json_response(
            {
                "discordId": str(entry["user_id"]),
                "discordUsername": entry["username"],
                "discordAvatar": entry["display_avatar_url"],
                "discordNickname": entry["nickname"],
                "discordJoinedAt": entry["guild_joined_at"],
            }
        )

    async def _handle_create_recruit_ticket(self, request: web.Request) -> web.Response:
        secret = config.get("verification.api_secret") or ""
        if request.headers.get("X-Api-Key") != secret:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid_body"}, status=400)

        discord_id_raw = str(payload.get("discordId", "")).strip()
        site_ticket_id = str(payload.get("siteTicketId", "")).strip()
        if not discord_id_raw or not discord_id_raw.isdigit() or not site_ticket_id:
            return web.json_response({"error": "invalid_body"}, status=400)

        subtype = payload.get("subtype") or "family"
        site_ticket_number = payload.get("siteTicketNumber")
        raw_fields = payload.get("fields") or []
        fields = [
            (str(pair[0])[:256], str(pair[1])[:1024])
            for pair in raw_fields
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ]

        from cogs.tickets import create_recruit_ticket_from_site

        result = await create_recruit_ticket_from_site(
            self.bot,
            int(discord_id_raw),
            subtype,
            site_ticket_id,
            site_ticket_number,
            fields,
        )
        if "error" in result:
            return web.json_response(result, status=422)
        return web.json_response(result)

    async def _handle_ticket_log(self, request: web.Request) -> web.Response:
        """Заменяет старый вебхук сайта (lib/discord-log.ts) — теперь лог-эмбеды в канал
        логов шлёт сам бот, поэтому они всегда попадают в правильный сервер, а не туда,
        куда смотрит забытый вебхук. channel: "tickets" -> log_channels.tickets,
        иначе -> channels.logs (тот же fallback, что и у log_channel_id() для других систем)."""
        secret = config.get("verification.api_secret") or ""
        if request.headers.get("X-Api-Key") != secret:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid_body"}, status=400)

        channel_key = payload.get("channel") or "general"
        channel_id = log_channel_id(channel_key)
        if not channel_id:
            return web.json_response({"error": "no_channel"}, status=503)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return web.json_response({"error": "channel_not_found"}, status=503)

        title = str(payload.get("title", "")).strip()
        color = _LOG_COLORS.get(payload.get("color"), _LOG_COLORS["info"])
        raw_fields = payload.get("fields") or []
        fields = [
            (str(f.get("name", ""))[:256], str(f.get("value", ""))[:1024], bool(f.get("inline", False)))
            for f in raw_fields
            if isinstance(f, dict)
        ]

        embed = base_embed(title, color=color, timestamp=True)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value or "—", inline=inline)

        message = None
        message_id = payload.get("messageId")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed)
            except (disnake.HTTPException, ValueError):
                message = None
        if message is None:
            try:
                message = await channel.send(embed=embed)
            except disnake.Forbidden:
                return web.json_response({"error": "forbidden"}, status=503)

        return web.json_response({"messageId": str(message.id)})


class Verify(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.api = VerifyAPI(bot)
        self._api_started = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._api_started:
            return
        self._api_started = True
        await self.api.start()

    def cog_unload(self) -> None:
        self.bot.loop.create_task(self.api.stop())

    @commands.slash_command(
        name="verify",
        description="Привязка аккаунта сайта к Discord",
        default_member_permissions=ADMIN_PERMS,
    )
    async def verify(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @verify.sub_command(name="show", description="Опубликовать панель привязки аккаунта в этом канале")
    async def verify_show(self, inter: disnake.ApplicationCommandInteraction):
        await send_panel(inter, _build_panel_embed(), view=VerifyPanelView(), panel_key="verify")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Verify(bot))


PERSISTENT_VIEWS = [lambda: VerifyPanelView()]
