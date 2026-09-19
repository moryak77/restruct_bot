from __future__ import annotations

import asyncio
import datetime as dt
import logging

import disnake
from disnake.ext import commands

from core.branding import FAMILY_NAME, _panel_image_path, base_embed, panel_file_kwargs
from core.config import GUILD_ID, config
from core.http import get_session
from core.icons import icon, icon_tag
from core.storage import coins_store

log = logging.getLogger("restruct-bot")

# Категория для тикетов R-Coins и канал модерации заявок (можно переопределить в config.json:
# coins.category_id / coins.moderation_channel_id).
DEFAULT_CATEGORY_ID = 1550120218066427965
DEFAULT_MODERATION_CHANNEL_ID = 1550822091413000203

# Через сколько секунд после решения удаляется канал тикета (успеть прочитать итог).
CLOSE_DELAY_SECONDS = 600


def _category_id() -> int:
    return int(config.get("coins.category_id") or DEFAULT_CATEGORY_ID)


def _moderation_channel_id() -> int:
    return int(config.get("coins.moderation_channel_id") or DEFAULT_MODERATION_CHANNEL_ID)


def _staff_role_ids() -> list[int]:
    """Кто видит тикеты и может модерировать: coins.moderator_role_ids, иначе роли рекрута."""
    explicit = config.get("coins.moderator_role_ids") or []
    if explicit:
        return [int(r) for r in explicit if r]
    recruit = config.get("tickets.recruit") or {}
    ids = [recruit.get("role_id")] + list(recruit.get("extra_role_ids") or [])
    return [int(r) for r in ids if r]


def _site_base_url() -> str:
    return (config.get("verification.site_base_url") or "").rstrip("/")


def _api_secret() -> str:
    return config.get("verification.api_secret") or ""


def _is_moderator(member: disnake.abc.User) -> bool:
    if not isinstance(member, disnake.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    staff = set(_staff_role_ids())
    return any(role.id in staff for role in member.roles)


def _attach_panel_thumbnail(embed: disnake.Embed) -> dict:
    """Картинка панели «coins» (/img) показывается миниатюрой — основное место занимают скриншоты."""
    path = _panel_image_path("coins")
    if path is None:
        return {}
    embed.set_thumbnail(url=f"attachment://panel_banner{path.suffix}")
    return panel_file_kwargs("coins")


def _proof_lines(urls: list[str]) -> str:
    return "\n".join(f"[Скриншот {i}]({url})" for i, url in enumerate(urls, start=1)) or "—"


def _claim_embed(claim: dict, *, status: str, color_status: str | None = None) -> disnake.Embed:
    embed = base_embed(f"{icon_tag('coin')} Заявка на R-Coins #{claim['number']}")
    embed.add_field(name="Участник", value=f"<@{claim['user_id']}>", inline=True)
    embed.add_field(name="Логин на сайте", value=claim["login"], inline=True)
    embed.add_field(name="Роль на сайте", value=claim["site_role"], inline=True)
    embed.add_field(name="Задание", value=claim["task"], inline=True)
    embed.add_field(name="Награда", value=f"**+{claim['coins']}** R-Coins", inline=True)
    embed.add_field(name="Доказательства", value=_proof_lines(claim["proofs"]), inline=False)
    embed.add_field(name="Статус", value=status, inline=False)
    if claim["proofs"]:
        embed.set_image(url=claim["proofs"][0])
    embed.set_footer(text=f"Заявка #{claim['number']} • Семья {FAMILY_NAME}")
    return embed


def _extra_proof_embeds(claim: dict) -> list[disnake.Embed]:
    return [
        base_embed(f"Скриншот {i}").set_image(url=url)
        for i, url in enumerate(claim["proofs"][1:4], start=2)
    ]


async def _site_decision(claim_id: str, decision: str, moderator: str, reason: str | None) -> tuple[int, dict]:
    if not _site_base_url() or not _api_secret():
        return 503, {"error": "site_not_configured"}
    try:
        async with get_session().post(
            f"{_site_base_url()}/api/discord/coins-decision",
            headers={"X-Api-Key": _api_secret()},
            json={"claimId": claim_id, "decision": decision, "moderatorName": moderator, "reason": reason},
            timeout=20,
        ) as resp:
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            return resp.status, body
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось передать решение по заявке R-Coins на сайт: %s", e)
        return 502, {"error": "site_unreachable"}


async def _delete_later(channel: disnake.abc.GuildChannel) -> None:
    await asyncio.sleep(CLOSE_DELAY_SECONDS)
    try:
        await channel.delete(reason="Заявка на R-Coins обработана")
    except disnake.HTTPException:
        pass


async def _finalize(
    bot: commands.InteractionBot,
    claim_id: str,
    decision: str,
    moderator: disnake.abc.User,
    reason: str | None,
    site_body: dict,
) -> None:
    data = coins_store.load()
    claim = data["claims"].get(claim_id)
    if claim is None:
        return

    approved = decision == "approved"
    status_text = (
        f"{icon_tag('check')} **Одобрено** — {moderator.mention}"
        if approved
        else f"{icon_tag('cross')} **Отклонено** — {moderator.mention}" + (f"\n**Причина:** {reason}" if reason else "")
    )

    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID.isdigit() else (bot.guilds[0] if bot.guilds else None)
    if guild is None:
        return

    # 1) Сообщение в канале модерации: итог и убираем кнопки.
    mod_channel = guild.get_channel(claim["mod_channel_id"])
    if mod_channel is not None:
        try:
            mod_message = await mod_channel.fetch_message(claim["mod_message_id"])
            await mod_message.edit(
                embeds=[_claim_embed(claim, status=status_text), *_extra_proof_embeds(claim)],
                view=None,
            )
        except disnake.HTTPException:
            pass

    # 2) Тикет-канал: итог, закрываем для писем и удаляем через некоторое время.
    channel = guild.get_channel(claim["channel_id"])
    if channel is not None:
        result_embed = base_embed(
            f"{icon_tag('check')} Заявка одобрена" if approved else f"{icon_tag('cross')} Заявка отклонена",
            (
                f"Начислено **+{claim['coins']} R-Coins**. Спасибо за активность!"
                if approved
                else "К сожалению, задание не засчитано." + (f"\n\n**Причина:** {reason}" if reason else "")
            ),
        )
        result_embed.add_field(name="Модератор", value=moderator.mention, inline=True)
        try:
            member = guild.get_member(claim["user_id"])
            await channel.send(content=member.mention if member else None, embed=result_embed)
            if member is not None:
                await channel.set_permissions(member, send_messages=False, reason="Заявка обработана")
            await channel.edit(name=f"coins-{claim['number']:04d}-{'ok' if approved else 'no'}")
        except disnake.HTTPException:
            pass
        asyncio.create_task(_delete_later(channel))

    # 3) Личное сообщение участнику.
    try:
        user = bot.get_user(claim["user_id"]) or await bot.fetch_user(claim["user_id"])
        await user.send(
            embed=base_embed(
                f"{icon_tag('check')} Заявка на R-Coins одобрена" if approved else f"{icon_tag('cross')} Заявка на R-Coins отклонена",
                (
                    f"Задание «{claim['task']}» засчитано: **+{claim['coins']} R-Coins** уже на твоём балансе."
                    if approved
                    else f"Задание «{claim['task']}» не засчитано." + (f"\n\n**Причина:** {reason}" if reason else "")
                ),
            )
        )
    except (disnake.HTTPException, AttributeError):
        pass


async def _handle_decision(
    inter: disnake.Interaction, claim_id: str, decision: str, reason: str | None
) -> None:
    """Общая логика кнопок «Одобрить»/«Отклонить». inter уже подтверждён (defer)."""
    status, body = await _site_decision(claim_id, decision, inter.author.display_name, reason)
    if status == 409:
        await inter.followup.send("Эта заявка уже обработана.", ephemeral=True)
        return
    if status != 200:
        await inter.followup.send(
            "Не удалось передать решение на сайт — попробуй ещё раз через минуту.", ephemeral=True
        )
        return
    await _finalize(inter.bot, claim_id, decision, inter.author, reason, body)
    await inter.followup.send(
        "Заявка одобрена — R-Coins начислены." if decision == "approved" else "Заявка отклонена.",
        ephemeral=True,
    )


class RejectModal(disnake.ui.Modal):
    def __init__(self, claim_id: str):
        self.claim_id = claim_id
        super().__init__(
            title="Причина отказа",
            custom_id=f"coins_reject_modal:{claim_id}",
            components=[
                disnake.ui.TextInput(
                    label="Причина (увидит участник)",
                    custom_id="reason",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=500,
                    required=True,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        await _handle_decision(inter, self.claim_id, "rejected", inter.text_values["reason"].strip())


def _claim_id_for(message: disnake.Message | None) -> str | None:
    if message is None:
        return None
    return coins_store.load()["by_message"].get(str(message.id))


class CoinClaimModView(disnake.ui.View):
    """Кнопки под заявкой в канале модерации. custom_id фиксированные, а нужная заявка
    определяется по id сообщения — поэтому кнопки работают и после перезапуска бота."""

    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Одобрить", style=disnake.ButtonStyle.success, emoji=icon("check"), custom_id="coins_approve")
    async def approve(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not _is_moderator(inter.author):
            await inter.response.send_message("Недостаточно прав для модерации R-Coins.", ephemeral=True)
            return
        claim_id = _claim_id_for(inter.message)
        if claim_id is None:
            await inter.response.send_message("Заявка не найдена (возможно, она устарела).", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        await _handle_decision(inter, claim_id, "approved", None)

    @disnake.ui.button(label="Отклонить", style=disnake.ButtonStyle.danger, emoji=icon("cross"), custom_id="coins_reject")
    async def reject(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not _is_moderator(inter.author):
            await inter.response.send_message("Недостаточно прав для модерации R-Coins.", ephemeral=True)
            return
        claim_id = _claim_id_for(inter.message)
        if claim_id is None:
            await inter.response.send_message("Заявка не найдена (возможно, она устарела).", ephemeral=True)
            return
        await inter.response.send_modal(RejectModal(claim_id))


async def create_coin_claim_from_site(bot: commands.InteractionBot, payload: dict) -> dict:
    """Заявка на R-Coins подана на сайте: создаёт тикет-канал участника в категории R-Coins и
    публикует заявку с кнопками в канале модерации."""
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID.isdigit() else (bot.guilds[0] if bot.guilds else None)
    if guild is None:
        return {"error": "bot_not_in_guild"}

    try:
        discord_id = int(str(payload.get("discordId", "")))
    except ValueError:
        return {"error": "invalid_body"}
    claim_id = str(payload.get("claimId", "")).strip()
    proofs = [str(u) for u in (payload.get("proofUrls") or []) if isinstance(u, str)][:4]
    if not claim_id or not proofs:
        return {"error": "invalid_body"}

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except disnake.HTTPException:
            return {"error": "member_not_found"}

    mod_channel = guild.get_channel(_moderation_channel_id())
    if mod_channel is None:
        log.warning("Канал модерации R-Coins %s не найден на сервере", _moderation_channel_id())
        return {"error": "moderation_channel_missing"}

    category = guild.get_channel(_category_id())
    overwrites = {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        member: disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
    }
    for role_id in _staff_role_ids():
        role = guild.get_role(role_id)
        if role is not None:
            overwrites[role] = disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    data = coins_store.load()
    data["counter"] += 1
    number = data["counter"]

    claim = {
        "id": claim_id,
        "number": number,
        "user_id": member.id,
        "login": str(payload.get("login") or "—")[:64],
        "site_role": str(payload.get("siteRole") or "—")[:32],
        "task": str(payload.get("task") or "—")[:32],
        "coins": int(payload.get("coins") or 0),
        "proofs": proofs,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    try:
        channel = await guild.create_text_channel(
            f"coins-{number:04d}",
            category=category if isinstance(category, disnake.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"Заявка на R-Coins с сайта ({member})",
        )
    except disnake.Forbidden:
        return {"error": "forbidden"}
    except disnake.HTTPException:
        return {"error": "channel_create_failed"}

    ticket_embed = _claim_embed(claim, status=f"{icon_tag('pending')} Заявка на проверке у модераторов. Ожидайте решения.")
    files = _attach_panel_thumbnail(ticket_embed)
    try:
        await channel.send(
            content=member.mention,
            embeds=[ticket_embed, *_extra_proof_embeds(claim)],
            allowed_mentions=disnake.AllowedMentions(users=True),
            **files,
        )

        mod_embed = _claim_embed(claim, status=f"{icon_tag('pending')} Ожидает модерации")
        mod_embed.add_field(name="Тикет", value=channel.mention, inline=True)
        mod_files = _attach_panel_thumbnail(mod_embed)
        mod_message = await mod_channel.send(
            embeds=[mod_embed, *_extra_proof_embeds(claim)],
            view=CoinClaimModView(),
            **mod_files,
        )
    except disnake.HTTPException as e:
        log.warning("Не удалось опубликовать заявку R-Coins: %s", e)
        try:
            await channel.delete(reason="Не удалось создать заявку на R-Coins")
        except disnake.HTTPException:
            pass
        return {"error": "publish_failed"}

    claim.update(
        {
            "channel_id": channel.id,
            "mod_channel_id": mod_channel.id,
            "mod_message_id": mod_message.id,
        }
    )
    data["claims"][claim_id] = claim
    data["by_message"][str(mod_message.id)] = claim_id
    coins_store.save(data)
    return {"channelId": str(channel.id)}


class Coins(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot


def setup(bot: commands.InteractionBot):
    bot.add_cog(Coins(bot))


PERSISTENT_VIEWS = [lambda: CoinClaimModView()]
