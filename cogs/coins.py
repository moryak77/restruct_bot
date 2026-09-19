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

# Через сколько секунд после решения тикет закрывается сам, если модератор не нажал «Закрыть».
AUTO_CLOSE_SECONDS = 1800
MAX_LISTED = 10

_list_lock = asyncio.Lock()


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


def _get_guild(bot: commands.InteractionBot) -> disnake.Guild | None:
    if GUILD_ID.isdigit():
        guild = bot.get_guild(int(GUILD_ID))
        if guild is not None:
            return guild
    return bot.guilds[0] if bot.guilds else None


def _open_claims(data: dict | None = None) -> list[tuple[str, dict]]:
    data = data or coins_store.load()
    items = [(cid, c) for cid, c in data["claims"].items() if not c.get("closed")]
    items.sort(key=lambda pair: pair[1]["number"])
    return items


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def _attach_panel_thumbnail(embed: disnake.Embed) -> dict:
    """Картинка панели «coins» (/img) показывается миниатюрой — основное место занимают скриншоты."""
    path = _panel_image_path("coins")
    if path is None:
        return {}
    embed.set_thumbnail(url=f"attachment://panel_banner{path.suffix}")
    return panel_file_kwargs("coins")


def _proof_lines(urls: list[str]) -> str:
    return "\n".join(f"[Скриншот {i}]({url})" for i, url in enumerate(urls, start=1)) or "—"


def _claim_embed(claim: dict, *, status: str) -> disnake.Embed:
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


def _status_text(claim: dict) -> str:
    decision = claim.get("decision")
    if decision == "approved":
        return f"{icon_tag('check')} Одобрено"
    if decision == "rejected":
        return f"{icon_tag('cross')} Отклонено"
    if claim.get("claimed_by"):
        return f"{icon_tag('pending')} В работе — <@{claim['claimed_by']}>"
    return f"{icon_tag('unassigned')} Не назначена"


def _build_list_embed(active: list[tuple[str, dict]]) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('clipboard')} Заявки на R-Coins",
        "Центр проверки заданий на R-Coins. Этот список обновляется автоматически — "
        "ничего не нужно делать вручную.",
        panel_key="coins",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            "**1.** Выберите заявку в меню под этим сообщением — откроется рабочая панель, "
            "видимая только вам.\n"
            f"**2.** Нажмите {icon_tag('hand')} **Забрать** — без этого решение принять нельзя.\n"
            "**3.** Проверьте скриншоты в тикете и отметьте "
            f"{icon_tag('check')} **Принять** (R-Coins начислятся сами) или {icon_tag('cross')} **Отклонить** "
            "(с указанием причины).\n"
            f"**4.** Нажмите {icon_tag('lock')} **Закрыть** — тикет закроется, а список обновится сам."
        ),
        inline=False,
    )

    if not active:
        embed.add_field(name=f"{icon_tag('clipboard')} Заявки", value="Сейчас нет открытых заявок.", inline=False)
        return embed

    lines = []
    for _, claim in active[:MAX_LISTED]:
        lines.append(
            f"`#{claim['number']}` **{claim['task']}** · **+{claim['coins']}** R-Coins\n"
            f"┗ <#{claim['channel_id']}> · <@{claim['user_id']}> · {_status_text(claim)}"
        )
    value = "\n\n".join(lines)
    if len(active) > MAX_LISTED:
        value += f"\n\n*...и ещё {len(active) - MAX_LISTED}*"
    embed.add_field(name=f"{icon_tag('clipboard')} Заявки ({len(active)})", value=value[:1024], inline=False)
    return embed


def _build_work_embed(claim: dict) -> disnake.Embed:
    embed = _claim_embed(claim, status=_status_text(claim))
    embed.add_field(name="Тикет", value=f"<#{claim['channel_id']}>", inline=True)
    claimed_by = claim.get("claimed_by")
    embed.add_field(
        name="Ответственный",
        value=f"<@{claimed_by}>" if claimed_by else f"{icon_tag('unassigned')} не назначен",
        inline=True,
    )
    hint = "" if claimed_by else " _(сначала заберите заявку)_"
    embed.add_field(
        name=f"{icon_tag('settings')} Кнопки",
        value=(
            f"{icon_tag('hand')} — **Забрать** заявку в обработку\n"
            f"{icon_tag('check')} — **Принять** и начислить R-Coins{hint}\n"
            f"{icon_tag('cross')} — **Отклонить** (с указанием причины){hint}\n"
            f"{icon_tag('lock')} — **Закрыть** тикет (после решения)"
        ),
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Список заявок (одно сообщение в канале модерации)
# ---------------------------------------------------------------------------

async def refresh_list(guild: disnake.Guild, channel: disnake.abc.GuildChannel | None = None) -> None:
    """Поддерживает единственное сообщение со списком заявок в актуальном состоянии: создаёт
    при первой необходимости, дальше только редактирует. Если передан `channel` (команда
    /rcoins show), панель переносится в этот канал, а прежнее сообщение удаляется."""
    async with _list_lock:
        data = coins_store.load()
        data.setdefault("list_message_id", None)
        data.setdefault("list_channel_id", None)

        if channel is not None:
            old_channel = guild.get_channel(data["list_channel_id"] or _moderation_channel_id())
            if old_channel is not None and data["list_message_id"]:
                try:
                    old_message = await old_channel.fetch_message(data["list_message_id"])
                    await old_message.delete()
                except disnake.HTTPException:
                    pass
            data["list_channel_id"] = channel.id
            data["list_message_id"] = None

        mod_channel = guild.get_channel(data["list_channel_id"] or _moderation_channel_id())
        if mod_channel is None:
            log.warning("Канал модерации R-Coins %s не найден", _moderation_channel_id())
            return

        # Миграция: раньше под каждую заявку публиковалось отдельное сообщение с кнопками.
        for claim in data["claims"].values():
            old_id = claim.pop("mod_message_id", None)
            if old_id:
                try:
                    old = await mod_channel.fetch_message(old_id)
                    await old.delete()
                except disnake.HTTPException:
                    pass
        data["by_message"] = {}

        # Заявки, чей канал удалили вручную, из списка убираем.
        for claim in data["claims"].values():
            if not claim.get("closed") and guild.get_channel(claim["channel_id"]) is None:
                claim["closed"] = True

        active = _open_claims(data)
        embed = _build_list_embed(active)
        view = CoinListView(active)

        message = None
        if data["list_message_id"]:
            try:
                message = await mod_channel.fetch_message(data["list_message_id"])
                await message.edit(embed=embed, view=view)
            except disnake.HTTPException:
                message = None
        if message is None:
            message = await mod_channel.send(embed=embed, view=view, **panel_file_kwargs("coins"))
            data["list_message_id"] = message.id
        coins_store.save(data)


class CoinListSelect(disnake.ui.StringSelect):
    def __init__(self, active: list[tuple[str, dict]]):
        if active:
            options = [
                disnake.SelectOption(
                    label=f"#{claim['number']} — {claim['task']} (+{claim['coins']})"[:100],
                    value=claim_id,
                    description=f"Открыл: {claim['login']}"[:100],
                    emoji=icon("coin"),
                )
                for claim_id, claim in active[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет активных заявок", value="none")]
        super().__init__(
            placeholder="Выберите заявку, чтобы начать работу",
            options=options,
            custom_id="coins_list_select",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет активных заявок.", ephemeral=True)
            return
        if not _is_moderator(inter.author):
            await inter.response.send_message("❌ Только сотрудники могут работать с заявками.", ephemeral=True)
            return

        claim = coins_store.load()["claims"].get(self.values[0])
        if claim is None or claim.get("closed"):
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            await refresh_list(inter.guild)
            return

        await inter.response.send_message(
            embeds=[_build_work_embed(claim), *_extra_proof_embeds(claim)],
            view=CoinWorkView(self.values[0]),
            ephemeral=True,
        )


class CoinListView(disnake.ui.View):
    def __init__(self, active: list[tuple[str, dict]]):
        super().__init__(timeout=None)
        self.add_item(CoinListSelect(active))


# ---------------------------------------------------------------------------
# Решение по заявке
# ---------------------------------------------------------------------------

async def _site_decision(claim_id: str, decision: str, moderator: str, reason: str | None) -> tuple[int, dict]:
    if not _site_base_url() or not _api_secret():
        return 503, {"error": "site_not_configured"}
    payload: dict = {"claimId": claim_id, "decision": decision, "moderatorName": moderator}
    if reason:
        payload["reason"] = reason
    try:
        async with get_session().post(
            f"{_site_base_url()}/api/discord/coins-decision",
            headers={"X-Api-Key": _api_secret()},
            json=payload,
            timeout=20,
        ) as resp:
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            if resp.status != 200:
                log.warning("Сайт вернул %s на решение по заявке R-Coins %s: %s", resp.status, claim_id, body)
            return resp.status, body
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось передать решение по заявке R-Coins на сайт: %s", e)
        return 502, {"error": "site_unreachable"}


async def _close_claim(bot: commands.InteractionBot, claim_id: str) -> None:
    """Закрывает заявку: удаляет тикет-канал и убирает её из списка."""
    data = coins_store.load()
    claim = data["claims"].get(claim_id)
    if claim is None or claim.get("closed"):
        return
    claim["closed"] = True
    coins_store.save(data)

    guild = _get_guild(bot)
    if guild is None:
        return
    channel = guild.get_channel(claim["channel_id"])
    if channel is not None:
        try:
            await channel.delete(reason="Заявка на R-Coins закрыта")
        except disnake.HTTPException:
            pass
    await refresh_list(guild)


async def _auto_close_later(bot: commands.InteractionBot, claim_id: str) -> None:
    await asyncio.sleep(AUTO_CLOSE_SECONDS)
    await _close_claim(bot, claim_id)


async def _apply_decision(
    inter: disnake.Interaction, claim_id: str, decision: str, reason: str | None
) -> tuple[bool, str]:
    """Передаёт решение сайту и, если он принял, оформляет итог в Discord. Возвращает
    (успех, сообщение для модератора)."""
    status, body = await _site_decision(claim_id, decision, inter.author.display_name, reason)
    if status == 409:
        return False, "Эта заявка уже обработана."
    if status != 200:
        return False, "Не удалось передать решение на сайт — попробуй ещё раз через минуту."

    bot = inter.bot
    data = coins_store.load()
    claim = data["claims"].get(claim_id)
    if claim is None:
        return True, "Решение сохранено."
    claim["decision"] = decision
    claim["decided_by"] = inter.author.id
    coins_store.save(data)
    approved = decision == "approved"

    guild = _get_guild(bot)
    if guild is not None:
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
            result_embed.add_field(name="Модератор", value=inter.author.mention, inline=True)
            try:
                member = guild.get_member(claim["user_id"])
                await channel.send(content=member.mention if member else None, embed=result_embed)
                if member is not None:
                    await channel.set_permissions(member, send_messages=False, reason="Заявка обработана")
                await channel.edit(name=f"coins-{claim['number']:04d}-{'ok' if approved else 'no'}")
            except disnake.HTTPException:
                pass
        await refresh_list(guild)

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

    asyncio.create_task(_auto_close_later(bot, claim_id))
    return True, "Заявка одобрена — R-Coins начислены." if approved else "Заявка отклонена."


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
        ok, text = await _apply_decision(inter, self.claim_id, "rejected", inter.text_values["reason"].strip())
        await inter.followup.send(text, ephemeral=True)


class CoinWorkView(disnake.ui.View):
    """Рабочая эфемерная панель одной заявки — открывается выбором в списке заявок. Видна только
    тому, кто её открыл; сам список при этом остаётся и обновляется."""

    def __init__(self, claim_id: str):
        super().__init__(timeout=600)
        self.claim_id = claim_id
        claim = coins_store.load()["claims"].get(claim_id)
        if claim is not None:
            if claim.get("claimed_by") is not None:
                _disable(self, "cwork_claim")
            if claim.get("decision") is not None:
                _disable(self, "cwork_claim", "cwork_accept", "cwork_reject")

    def _claim(self) -> dict | None:
        claim = coins_store.load()["claims"].get(self.claim_id)
        return None if claim is None or claim.get("closed") else claim

    async def _guard(self, inter: disnake.MessageInteraction) -> dict | None:
        if not _is_moderator(inter.author):
            await inter.response.send_message("❌ Только сотрудники могут работать с заявками.", ephemeral=True)
            return None
        claim = self._claim()
        if claim is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return None
        return claim

    def _can_decide(self, inter: disnake.MessageInteraction, claim: dict) -> str | None:
        if claim.get("decision"):
            return "Эта заявка уже обработана."
        claimed_by = claim.get("claimed_by")
        if claimed_by is None:
            return "Сначала заберите заявку — нажмите «Забрать»."
        if claimed_by != inter.author.id and not inter.author.guild_permissions.manage_guild:
            return f"Заявку ведёт <@{claimed_by}> — решение принимает он."
        return None

    @disnake.ui.button(label="Забрать", emoji=icon("hand"), style=disnake.ButtonStyle.secondary, custom_id="cwork_claim", row=0)
    async def claim_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        claim = await self._guard(inter)
        if claim is None:
            return
        if claim.get("claimed_by") is not None:
            await inter.response.send_message(f"❌ Заявку уже забрал <@{claim['claimed_by']}>.", ephemeral=True)
            return

        data = coins_store.load()
        data["claims"][self.claim_id]["claimed_by"] = inter.author.id
        coins_store.save(data)
        claim = data["claims"][self.claim_id]

        _disable(self, "cwork_claim")
        await inter.response.edit_message(embeds=[_build_work_embed(claim), *_extra_proof_embeds(claim)], view=self)
        await refresh_list(inter.guild)

        channel = inter.guild.get_channel(claim["channel_id"])
        if channel is not None:
            try:
                await channel.send(
                    content=f"<@{claim['user_id']}>",
                    embed=base_embed(
                        f"{icon_tag('pending')} Заявка взята в обработку",
                        f"Твою заявку проверяет {inter.author.mention}. Решение придёт сюда и в личные сообщения.",
                    ),
                )
            except disnake.HTTPException:
                pass

    @disnake.ui.button(label="Принять", emoji=icon("check"), style=disnake.ButtonStyle.success, custom_id="cwork_accept", row=0)
    async def accept_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        claim = await self._guard(inter)
        if claim is None:
            return
        problem = self._can_decide(inter, claim)
        if problem:
            await inter.response.send_message(f"❌ {problem}", ephemeral=True)
            return

        await inter.response.defer()
        ok, text = await _apply_decision(inter, self.claim_id, "approved", None)
        if ok:
            fresh = coins_store.load()["claims"].get(self.claim_id, claim)
            _disable(self, "cwork_claim", "cwork_accept", "cwork_reject")
            await inter.edit_original_response(embeds=[_build_work_embed(fresh), *_extra_proof_embeds(fresh)], view=self)
        await inter.followup.send(text, ephemeral=True)

    @disnake.ui.button(label="Отклонить", emoji=icon("cross"), style=disnake.ButtonStyle.danger, custom_id="cwork_reject", row=0)
    async def reject_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        claim = await self._guard(inter)
        if claim is None:
            return
        problem = self._can_decide(inter, claim)
        if problem:
            await inter.response.send_message(f"❌ {problem}", ephemeral=True)
            return
        await inter.response.send_modal(RejectModal(self.claim_id))

    @disnake.ui.button(label="Закрыть", emoji=icon("lock"), style=disnake.ButtonStyle.secondary, custom_id="cwork_close", row=1)
    async def close_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        claim = await self._guard(inter)
        if claim is None:
            return
        if not claim.get("decision"):
            await inter.response.send_message("❌ Закрыть можно только после решения (принять или отклонить).", ephemeral=True)
            return
        await inter.response.defer()
        await _close_claim(inter.bot, self.claim_id)
        await inter.edit_original_response(
            embed=base_embed(f"{icon_tag('lock')} Заявка закрыта", "Тикет удалён, список заявок обновлён."), view=None
        )


def _disable(view: disnake.ui.View, *custom_ids: str) -> None:
    for child in view.children:
        if getattr(child, "custom_id", None) in custom_ids:
            child.disabled = True


# ---------------------------------------------------------------------------
# Создание заявки с сайта
# ---------------------------------------------------------------------------

async def create_coin_claim_from_site(bot: commands.InteractionBot, payload: dict) -> dict:
    """Заявка на R-Coins подана на сайте: создаёт тикет-канал участника в категории R-Coins и
    добавляет заявку в список модерации."""
    guild = _get_guild(bot)
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

    if guild.get_channel(_moderation_channel_id()) is None:
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
        "claimed_by": None,
        "decision": None,
        "closed": False,
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
    except disnake.HTTPException as e:
        log.warning("Не удалось опубликовать заявку R-Coins: %s", e)
        try:
            await channel.delete(reason="Не удалось создать заявку на R-Coins")
        except disnake.HTTPException:
            pass
        return {"error": "publish_failed"}

    claim["channel_id"] = channel.id
    data["claims"][claim_id] = claim
    coins_store.save(data)

    try:
        await refresh_list(guild)
    except disnake.HTTPException as e:
        log.warning("Не удалось обновить список заявок R-Coins: %s", e)
    return {"channelId": str(channel.id)}


class Coins(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._started = False

    @commands.slash_command(
        name="rcoins",
        description="Модерация заявок на R-Coins",
        default_member_permissions=disnake.Permissions(manage_guild=True),
    )
    async def rcoins(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @rcoins.sub_command(name="show", description="Опубликовать панель модерации заявок R-Coins в этом канале")
    async def rcoins_show(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        await refresh_list(inter.guild, inter.channel)
        await inter.followup.send("✅ Панель заявок R-Coins опубликована в этом канале.", ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        # Панель должна быть в канале модерации сразу, а не после первой заявки.
        if self._started:
            return
        self._started = True
        guild = _get_guild(self.bot)
        if guild is None:
            return
        try:
            await refresh_list(guild)
        except disnake.HTTPException as e:
            log.warning("Не удалось обновить панель заявок R-Coins при старте: %s", e)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Coins(bot))


PERSISTENT_VIEWS = [lambda: CoinListView(_open_claims())]
