from __future__ import annotations

import datetime as dt
import re

import disnake
from disnake.ext import commands, tasks

from cogs.warns import UnwarnSelect, _active_warns, issue_warn, lift_warn
from core.branding import FAMILY_NAME, base_embed, send_panel
from core.config import config
from core.icons import icon, icon_tag
from core.storage import moderation_store, warns_store

EXPIRY_CHECK_MINUTES = 5
MAX_MUTE_DURATION = dt.timedelta(days=28)  # хард-лимит Discord на timeout — длиннее API не примет
REQUEST_CARD_LIFETIME = 30  # секунд — сколько карточка заявки висит в канале после решения
DURATION_HELP = "Формат: число + d/h/m, можно сочетать — например `1d`, `12h`, `45m`, `3d12h`."

PUNISHMENT_LABELS = {
    "ban": "Бан (временный)",
    "unban": "Разбан",
    "permban": "Перманентный бан",
    "unpermban": "Снятие перманентного бана",
    "kick": "Кик",
    "warn": "Предупреждение",
    "unwarn": "Снятие предупреждения",
}
PUNISHMENT_ICONS = {
    "ban": "gavel",
    "unban": "check",
    "permban": "ban",
    "unpermban": "check",
    "kick": "kick",
    "warn": "alert",
    "unwarn": "check",
}
ALL_TYPES = ("ban", "permban", "unban", "unpermban", "kick", "warn", "unwarn")
# Типы, которые можно выдать в один клик через контекстное меню участника (правый клик по
# нику) — только те, что нацелены на текущего участника сервера. unban/unpermban сюда не
# входят: забаненный/бывший участник по определению не отображается в списке для right-click.
CONTEXT_TARGET_TYPES = ("ban", "permban", "kick", "warn", "unwarn")
NEEDS_DURATION = {"ban"}
NEEDS_EVIDENCE = {"ban", "permban", "kick"}  # разбан/снятие пермбана/предупреждения доказательств не требуют

_DURATION_RE = re.compile(r"(\d+)([dhm])")
_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


# ---------------------------------------------------------------------------
# Права, парсинг, общие хелперы
# ---------------------------------------------------------------------------

def _leader_role_ids() -> list[int]:
    return [i for i in (config.get("moderation.leader_role_ids", []) or []) if i]


def _is_leader(member: disnake.Member) -> bool:
    """own/dep.own — прямой доступ к /ban /unban /permban /unpermban /kick, и они же
    решают по заявкам на наказание в канале модерации."""
    if member.guild_permissions.manage_guild:
        return True
    role_ids = _leader_role_ids()
    member_role_ids = {r.id for r in member.roles}
    return any(rid in member_role_ids for rid in role_ids)


def _can_mute(member: disnake.Member) -> bool:
    """/mute и /unmute доступны роли moderation.mute_role_id и выше по иерархии сервера —
    не только этой конкретной роли, а ей и всем, кто выше неё в списке ролей."""
    if member.guild_permissions.manage_guild:
        return True
    role_id = config.get("moderation.mute_role_id")
    if not role_id:
        return False
    threshold = member.guild.get_role(role_id)
    if threshold is None:
        return False
    return member.top_role.position >= threshold.position


def _can_request(member: disnake.Member) -> bool:
    """Подавать заявку на наказание может только moderation.requester_role_id."""
    if member.guild_permissions.manage_guild:
        return True
    role_id = config.get("moderation.requester_role_id")
    return bool(role_id) and role_id in {r.id for r in member.roles}


def _parse_duration(raw: str) -> dt.timedelta | None:
    """Принимает компактный формат вида '1d12h30m', '2h', '45m', '3d' — число сразу
    перед d(день)/h(час)/m(минута), можно комбинировать. Отклоняет любой мусор в строке,
    а не просто игнорирует нераспознанные символы."""
    text = raw.strip().lower().replace(" ", "")
    if not text:
        return None
    matches = _DURATION_RE.findall(text)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != text:
        return None

    total_minutes = 0
    for number, unit in matches:
        value = int(number)
        if unit == "d":
            total_minutes += value * 24 * 60
        elif unit == "h":
            total_minutes += value * 60
        else:
            total_minutes += value
    if total_minutes <= 0:
        return None
    return dt.timedelta(minutes=total_minutes)


def _format_duration(delta: dt.timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    return " ".join(parts) or "0м"


def _parse_user_id(raw: str) -> int | None:
    text = raw.strip()
    match = _MENTION_RE.match(text)
    if match:
        return int(match.group(1))
    if text.isdigit():
        return int(text)
    return None


def _dm_note(dm_ok: bool) -> str:
    return "Уведомление в ЛС отправлено." if dm_ok else "Не удалось отправить уведомление в ЛС."


async def _banned_user_autocomplete(inter: disnake.ApplicationCommandInteraction, query: str) -> dict[str, str]:
    """Подсказка для /unban и /unpermban — печатать ID забаненного вручную не нужно,
    достаточно начать вводить ник и выбрать из уже забаненных."""
    query = query.strip().lower()
    results: dict[str, str] = {}
    async for ban_entry in inter.guild.bans(limit=1000):
        user = ban_entry.user
        label = str(user)
        if query and query not in label.lower() and query not in str(user.id):
            continue
        results[f"{label} (ID {user.id})"[:100]] = str(user.id)
        if len(results) >= 25:
            break
    return results


def _leader_mentions() -> str:
    return " ".join(f"<@&{rid}>" for rid in _leader_role_ids())


async def _log_moderation(guild: disnake.Guild, embed: disnake.Embed) -> None:
    channel_id = config.get("moderation.log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(
            content=_leader_mentions() or None,
            embed=embed,
            allowed_mentions=disnake.AllowedMentions(roles=True),
        )
    except disnake.HTTPException:
        pass


def _build_action_log_embed(
    title: str,
    target: disnake.abc.User | disnake.Object,
    executor_mention: str,
    reason: str,
    *,
    until: dt.datetime | None = None,
    request_id: int | None = None,
    requested_by_id: int | None = None,
    evidence: str | None = None,
) -> disnake.Embed:
    """Полная карточка наказания в лог-канале — участник (с аватаркой), кто выдал, до какого
    времени (если применимо), откуда взялось решение (прямая команда или конкретная заявка
    с указанием, кто её подал), причина и доказательства отдельными, явно подписанными
    блоками — а не одной сплошной простынёй текста."""
    target_mention = target.mention if hasattr(target, "mention") else f"<@{target.id}>"
    target_display = str(target) if not isinstance(target, disnake.Object) else f"ID {target.id}"

    embed = base_embed(title, color=disnake.Color.dark_grey(), timestamp=True)
    avatar = getattr(target, "display_avatar", None)
    if avatar is not None:
        embed.set_thumbnail(url=avatar.url)

    embed.add_field(name=f"{icon_tag('users')} Участник", value=f"{target_mention}\n`{target_display}`", inline=True)
    embed.add_field(name="🆔 ID участника", value=f"`{target.id}`", inline=True)
    embed.add_field(name=f"{icon_tag('shield')} Кто выдал", value=executor_mention, inline=True)

    if until is not None:
        embed.add_field(
            name=f"{icon_tag('pending')} Действует до",
            value=f"<t:{int(until.timestamp())}:f>\n<t:{int(until.timestamp())}:R>",
            inline=True,
        )

    if request_id is not None:
        source_value = f"Заявка #{request_id}"
        if requested_by_id is not None:
            source_value += f"\nПодал: <@{requested_by_id}>"
    else:
        source_value = "Прямая команда руководства"
    embed.add_field(name=f"{icon_tag('clipboard')} Источник", value=source_value, inline=True)

    embed.add_field(name=f"{icon_tag('help')} Причина", value=(reason or "—")[:1024], inline=False)
    if evidence:
        embed.add_field(name="🔍 Доказательства", value=evidence[:1024], inline=False)
    return embed


# ---------------------------------------------------------------------------
# Исполнение наказаний — общая логика для прямых команд и одобренных заявок,
# чтобы оба пути гарантированно вели себя одинаково.
# ---------------------------------------------------------------------------

async def _notify_target(user: disnake.abc.Snowflake, embed: disnake.Embed) -> bool:
    """Best-effort ЛС цели — не у всех объектов (например disnake.Object при разбане
    покинувшего сервер пользователя) вообще есть .send(), и ЛС может быть закрыто."""
    if not hasattr(user, "send"):
        return False
    try:
        await user.send(embed=embed)
        return True
    except (disnake.Forbidden, disnake.HTTPException):
        return False


async def _do_ban(
    guild: disnake.Guild,
    user: disnake.User | disnake.Member,
    delta: dt.timedelta | None,
    reason: str,
    executor: disnake.abc.User,
    *,
    request_id: int | None = None,
    requested_by_id: int | None = None,
    evidence: str | None = None,
) -> tuple[bool, str | None, bool]:
    until = dt.datetime.now(dt.timezone.utc) + delta if delta is not None else None

    # ЛС отправляем до самого бана — пока цель ещё видит сервер, шанс, что у бота и
    # пользователя есть общий сервер (обязательное условие для ЛС от бота), максимальный.
    dm_embed = base_embed(
        f"{icon_tag('gavel')} Вы временно забанены" if until is not None else f"{icon_tag('ban')} Вы забанены навсегда",
        f"На сервере семьи **{guild.name}** вам выдан бан.",
        color=disnake.Color.red(),
    )
    dm_embed.add_field(name="Причина", value=reason or "—", inline=False)
    if until is not None:
        dm_embed.add_field(name="Действует до", value=f"<t:{int(until.timestamp())}:f>", inline=False)
    dm_ok = await _notify_target(user, dm_embed)

    try:
        await guild.ban(user, reason=reason[:400])
    except disnake.Forbidden:
        return False, "не хватает прав (роль бота ниже цели в иерархии)", dm_ok
    except disnake.HTTPException as e:
        return False, f"ошибка Discord ({e})", dm_ok

    data = moderation_store.load()
    if delta is not None:
        unban_at = dt.datetime.now(dt.timezone.utc) + delta
        data["temp_bans"][str(user.id)] = {
            "guild_id": guild.id,
            "unban_at": unban_at.isoformat(),
            "reason": reason,
            "banned_by": executor.id,
        }
    else:
        data["temp_bans"].pop(str(user.id), None)
    moderation_store.save(data)

    title = f"{icon_tag('gavel')} Бан (временный)" if delta is not None else f"{icon_tag('ban')} Перманентный бан"
    await _log_moderation(
        guild,
        _build_action_log_embed(
            title, user, executor.mention, reason,
            until=until, request_id=request_id, requested_by_id=requested_by_id, evidence=evidence,
        ),
    )
    return True, None, dm_ok


async def _do_unban(
    guild: disnake.Guild,
    user: disnake.User | disnake.Object,
    reason: str,
    executor: disnake.abc.User,
    *,
    permanent_label: bool,
    request_id: int | None = None,
    requested_by_id: int | None = None,
) -> tuple[bool, str | None, bool]:
    try:
        await guild.unban(user, reason=reason[:400])
    except disnake.NotFound:
        return False, "этот пользователь не забанен", False
    except disnake.Forbidden:
        return False, "не хватает прав", False
    except disnake.HTTPException as e:
        return False, f"ошибка Discord ({e})", False

    data = moderation_store.load()
    data["temp_bans"].pop(str(user.id), None)
    moderation_store.save(data)

    title = f"{icon_tag('check')} Снят перманентный бан" if permanent_label else f"{icon_tag('check')} Снят бан"
    dm_embed = base_embed(
        f"{icon_tag('check')} С вас снят бан",
        f"На сервере семьи **{guild.name}** с вас снят бан — доступ восстановлен.",
        color=disnake.Color.green(),
    )
    dm_embed.add_field(name="Причина", value=reason or "—", inline=False)
    # Забаненный пользователь почти наверняка не имеет общего сервера с ботом, поэтому ЛС
    # часто не доставляется — это ожидаемо и не считается ошибкой самого разбана.
    dm_ok = await _notify_target(user, dm_embed)

    await _log_moderation(
        guild,
        _build_action_log_embed(
            title, user, executor.mention, reason, request_id=request_id, requested_by_id=requested_by_id,
        ),
    )
    return True, None, dm_ok


async def _do_kick(
    member: disnake.Member,
    reason: str,
    executor: disnake.abc.User,
    *,
    request_id: int | None = None,
    requested_by_id: int | None = None,
    evidence: str | None = None,
) -> tuple[bool, str | None]:
    try:
        await member.kick(reason=reason[:400])
    except disnake.Forbidden:
        return False, "не хватает прав (роль бота ниже цели в иерархии)"
    except disnake.HTTPException as e:
        return False, f"ошибка Discord ({e})"
    await _log_moderation(
        member.guild,
        _build_action_log_embed(
            f"{icon_tag('kick')} Кик", member, executor.mention, reason,
            request_id=request_id, requested_by_id=requested_by_id, evidence=evidence,
        ),
    )
    return True, None


async def _do_mute(member: disnake.Member, delta: dt.timedelta, reason: str, executor: disnake.abc.User) -> tuple[bool, str | None]:
    try:
        await member.timeout(duration=delta, reason=reason[:400])
    except disnake.Forbidden:
        return False, "не хватает прав (роль бота ниже цели в иерархии)"
    except disnake.HTTPException as e:
        return False, f"ошибка Discord ({e})"
    until = dt.datetime.now(dt.timezone.utc) + delta
    await _log_moderation(member.guild, _build_action_log_embed(f"{icon_tag('lock')} Мут", member, executor.mention, reason, until=until))
    return True, None


async def _do_unmute(member: disnake.Member, reason: str, executor: disnake.abc.User) -> tuple[bool, str | None]:
    try:
        await member.timeout(duration=None, reason=reason[:400])
    except disnake.Forbidden:
        return False, "не хватает прав"
    except disnake.HTTPException as e:
        return False, f"ошибка Discord ({e})"
    await _log_moderation(member.guild, _build_action_log_embed(f"{icon_tag('check')} Снят мут", member, executor.mention, reason))
    return True, None


async def _execute_request(inter: disnake.Interaction, request: dict) -> tuple[bool, str | None]:
    """Исполняет одобренную заявку — резолвит цель под конкретный тип наказания и вызывает
    тот же _do_* хелпер, что и соответствующая прямая команда. Причина в логе остаётся
    чистой (не склеена с метаданными) — источник, автор заявки и доказательства идут
    отдельными полями через _build_action_log_embed."""
    guild = inter.guild
    target_id = request["target_id"]
    ptype = request["type"]
    reason = request["reason"]
    extras = {
        "request_id": request["id"],
        "requested_by_id": request["requested_by"],
    }

    if ptype in ("ban", "permban"):
        target = guild.get_member(target_id)
        if target is None:
            try:
                target = await inter.bot.fetch_user(target_id)
            except disnake.NotFound:
                return False, "пользователь не найден в Discord"
        delta = dt.timedelta(minutes=request["duration_minutes"]) if request.get("duration_minutes") else None
        ok, error, _dm_ok = await _do_ban(guild, target, delta, reason, inter.author, evidence=request.get("evidence"), **extras)
        return ok, error

    if ptype in ("unban", "unpermban"):
        try:
            target = await inter.bot.fetch_user(target_id)
        except disnake.NotFound:
            target = disnake.Object(id=target_id)
        ok, error, _dm_ok = await _do_unban(guild, target, reason, inter.author, permanent_label=(ptype == "unpermban"), **extras)
        return ok, error

    if ptype == "kick":
        target = guild.get_member(target_id)
        if target is None:
            return False, "участник уже не на сервере"
        return await _do_kick(target, reason, inter.author, evidence=request.get("evidence"), **extras)

    if ptype == "warn":
        target = guild.get_member(target_id)
        if target is None:
            return False, "участник не найден на сервере"
        await issue_warn(guild, target, inter.author, reason)
        await _log_moderation(
            guild,
            _build_action_log_embed(f"{icon_tag('alert')} Предупреждение", target, inter.author.mention, reason, **extras),
        )
        return True, None

    if ptype == "unwarn":
        active = _active_warns(guild.id, target_id)
        if not active:
            return False, "у пользователя нет активных предупреждений"
        # Заявка на снятие не указывает конкретный номер — снимаем самое старое активное
        # предупреждение (первое по очереди нарушение). Если нужно снять конкретное, у
        # руководства есть /unwarn с выбором из списка.
        oldest_id = min(w["id"] for w in active)
        try:
            target = await inter.bot.fetch_user(target_id)
        except disnake.HTTPException:
            target = disnake.Object(id=target_id)
        await lift_warn(guild, target, inter.author, oldest_id)
        await _log_moderation(
            guild,
            _build_action_log_embed(f"{icon_tag('check')} Снято предупреждение", target, inter.author.mention, reason, **extras),
        )
        return True, None

    return False, "неизвестный тип наказания"


def _find_request_by_message(message_id: int) -> tuple[dict | None, int | None]:
    data = moderation_store.load()
    for request_id_str, request in data["requests"].items():
        if request.get("message_id") == message_id:
            return request, int(request_id_str)
    return None, None


def _replace_status_field(embed: disnake.Embed, name: str, value: str) -> None:
    """Статус всегда добавляется последним полем при создании заявки — заменяем именно
    последнее поле, а не ищем по названию (оно может отличаться по языку/эмодзи)."""
    if embed.fields:
        embed.set_field_at(len(embed.fields) - 1, name=name, value=value, inline=False)
    else:
        embed.add_field(name=name, value=value, inline=False)


def _build_request_embed(request: dict, target: disnake.abc.User, requester: disnake.abc.User) -> disnake.Embed:
    embed = base_embed(
        f"📋 Заявка на наказание: {PUNISHMENT_LABELS[request['type']]}",
        color=disnake.Color.orange(),
        timestamp=True,
        footer_text=f"Заявка #{request['id']} • Семья {FAMILY_NAME}",
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Игрок", value=f"{target.mention}\n`{target}`", inline=True)
    embed.add_field(name="Запросил", value=requester.mention, inline=True)
    if request.get("duration_minutes"):
        embed.add_field(name="Срок", value=_format_duration(dt.timedelta(minutes=request["duration_minutes"])), inline=True)
    embed.add_field(name="Причина", value=request["reason"], inline=False)
    if request.get("evidence"):
        embed.add_field(name="Доказательства", value=request["evidence"][:1024], inline=False)
    embed.add_field(name=f"{icon_tag('pending')} Статус", value="Ожидает решения руководства", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Панель заявки на наказание (для moderation.requester_role_id)
# ---------------------------------------------------------------------------

class PunishmentRequestModal(disnake.ui.Modal):
    """Игрок либо вводится текстом (панель в канале заявок), либо уже известен заранее —
    когда модалка открыта через контекстное меню участника (правый клик → тег, без набора
    ID вручную): тогда поле «Игрок» из анкеты просто убирается."""

    def __init__(self, punishment_type: str, *, target: disnake.abc.User | None = None):
        self.punishment_type = punishment_type
        self.target = target
        components = []
        if target is None:
            components.append(
                disnake.ui.TextInput(
                    label="Игрок (упоминание или ID)",
                    custom_id="target",
                    style=disnake.TextInputStyle.short,
                    max_length=100,
                    placeholder="Например: @Никнейм или 538589009083768863",
                ),
            )
        if punishment_type in NEEDS_DURATION:
            components.append(
                disnake.ui.TextInput(
                    label="Срок в днях (только цифра)",
                    custom_id="duration",
                    style=disnake.TextInputStyle.short,
                    max_length=3,
                    placeholder="Например: 7",
                )
            )
        components.append(
            disnake.ui.TextInput(
                label="Причина",
                custom_id="reason",
                style=disnake.TextInputStyle.paragraph,
                max_length=400,
            )
        )
        if punishment_type in NEEDS_EVIDENCE:
            components.append(
                disnake.ui.TextInput(
                    label="Доказательства (ссылка/описание)",
                    custom_id="evidence",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=400,
                    required=False,
                )
            )
        super().__init__(title=f"Заявка: {PUNISHMENT_LABELS[punishment_type]}"[:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)

        if self.target is not None:
            target = self.target
        else:
            user_id = _parse_user_id(inter.text_values["target"])
            if user_id is None:
                await inter.followup.send("❌ Не удалось распознать игрока — укажите упоминание (@ник) или числовой ID.", ephemeral=True)
                return

            if self.punishment_type in ("unban", "unpermban"):
                try:
                    ban_entry = await inter.guild.fetch_ban(disnake.Object(id=user_id))
                except disnake.NotFound:
                    await inter.followup.send("❌ Этот пользователь не забанен на сервере.", ephemeral=True)
                    return
                except disnake.HTTPException:
                    await inter.followup.send("❌ Не удалось проверить бан-лист — попробуйте позже.", ephemeral=True)
                    return
                target = ban_entry.user
            elif self.punishment_type in ("kick", "warn", "unwarn"):
                target = inter.guild.get_member(user_id)
                if target is None:
                    await inter.followup.send("❌ Этот участник не найден на сервере.", ephemeral=True)
                    return
            else:  # ban / permban
                target = inter.guild.get_member(user_id)
                if target is None:
                    try:
                        target = await inter.bot.fetch_user(user_id)
                    except disnake.NotFound:
                        await inter.followup.send("❌ Пользователь с таким ID не найден в Discord.", ephemeral=True)
                        return

        if self.punishment_type == "unwarn" and not _active_warns(inter.guild_id, target.id):
            await inter.followup.send("❌ У этого участника нет активных предупреждений.", ephemeral=True)
            return

        if target.id == inter.author.id:
            await inter.followup.send("❌ Нельзя подать заявку на самого себя.", ephemeral=True)
            return
        if target.bot:
            await inter.followup.send("❌ Нельзя подать заявку на бота.", ephemeral=True)
            return

        delta = None
        if self.punishment_type in NEEDS_DURATION:
            days_raw = inter.text_values["duration"].strip()
            if not days_raw.isdigit() or not (1 <= int(days_raw) <= 365):
                await inter.followup.send("❌ Срок — целое число дней от 1 до 365, без букв и дробей.", ephemeral=True)
                return
            delta = dt.timedelta(days=int(days_raw))

        channel_id = config.get("moderation.request_channel_id")
        channel = inter.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            await inter.followup.send("❌ Канал для заявок на наказание не настроен — обратитесь к разработчику.", ephemeral=True)
            return

        data = moderation_store.load()
        request_id = data["next_id"]
        data["next_id"] += 1
        request = {
            "id": request_id,
            "type": self.punishment_type,
            "target_id": target.id,
            "duration_minutes": int(delta.total_seconds() // 60) if delta is not None else None,
            "reason": inter.text_values["reason"].strip(),
            "evidence": inter.text_values.get("evidence", "").strip() or None,
            "requested_by": inter.author.id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "pending",
        }
        data["requests"][str(request_id)] = request
        moderation_store.save(data)

        embed = _build_request_embed(request, target, inter.author)
        try:
            message = await channel.send(
                content=_leader_mentions() or None,
                embed=embed,
                view=PunishmentReviewView(),
                allowed_mentions=disnake.AllowedMentions(roles=True),
            )
        except disnake.HTTPException:
            await inter.followup.send("❌ Не удалось опубликовать заявку — проверьте права бота в канале заявок.", ephemeral=True)
            return

        data = moderation_store.load()
        data["requests"][str(request_id)]["message_id"] = message.id
        data["requests"][str(request_id)]["channel_id"] = message.channel.id
        moderation_store.save(data)

        await inter.followup.send(f"✅ Заявка отправлена на рассмотрение в {channel.mention}.", ephemeral=True)


class PunishmentTypeSelect(disnake.ui.StringSelect):
    """Select-меню (не кнопки — так и должно быть). У Discord есть известный клиентский
    баг: select-меню, которое в ответ открывает модалку, может «залипнуть» на последнем
    выборе и не среагировать на повторный клик по тому же пункту, потому что клиент не
    получает от бота никакого обновления самого меню (весь ответ уходит на модалку).
    Лечится так: помимо самой модалки, сразу отдельным вызовом (не через inter.response —
    он уже занят модалкой, а напрямую через inter.message.edit) переиздаём сообщение со
    свежим экземпляром view — это принудительно сбрасывает визуальное состояние меню."""

    def __init__(self, *, custom_id: str = "punish_type_select_once"):
        options = [
            disnake.SelectOption(label=PUNISHMENT_LABELS[t], value=t, emoji=icon(PUNISHMENT_ICONS[t]))
            for t in ALL_TYPES
        ]
        super().__init__(placeholder="Выберите тип наказания", options=options, custom_id=custom_id)

    async def callback(self, inter: disnake.MessageInteraction):
        if not _can_request(inter.author):
            await inter.response.send_message(
                "❌ Подавать заявки на наказание может только соответствующая роль.", ephemeral=True
            )
            return

        punishment_type = self.values[0]
        if punishment_type in ("unban", "unpermban"):
            await _respond_with_banned_user_picker(inter, punishment_type)
        else:
            await inter.response.send_modal(PunishmentRequestModal(punishment_type))

        if inter.message is not None:
            try:
                await inter.message.edit(view=self.view.__class__())
            except disnake.HTTPException:
                pass


class BannedUserSelect(disnake.ui.StringSelect):
    """Список уже забаненных участников вместо ручного набора ID/упоминания — надёжнее
    (нельзя опечататься или указать не того, кто не забанен) и быстрее."""

    def __init__(self, ban_entries: list[disnake.BanEntry], punishment_type: str):
        self.punishment_type = punishment_type
        self.users_by_id = {str(entry.user.id): entry.user for entry in ban_entries}
        options = [
            disnake.SelectOption(
                label=str(entry.user)[:100],
                value=str(entry.user.id),
                description=(entry.reason or "без причины в бан-листе")[:100],
            )
            for entry in ban_entries
        ]
        super().__init__(placeholder="Выберите забаненного участника", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        if not _can_request(inter.author):
            await inter.response.send_message(
                "❌ Подавать заявки на наказание может только соответствующая роль.", ephemeral=True
            )
            return
        target = self.users_by_id[self.values[0]]
        await inter.response.send_modal(PunishmentRequestModal(self.punishment_type, target=target))


class BannedUserView(disnake.ui.View):
    def __init__(self, ban_entries: list[disnake.BanEntry], punishment_type: str):
        super().__init__(timeout=180)
        self.add_item(BannedUserSelect(ban_entries, punishment_type))


async def _respond_with_banned_user_picker(inter: disnake.MessageInteraction, punishment_type: str) -> None:
    # limit=26, а не 25 (предел размера select-меню Discord) — специально, чтобы отличить
    # «ровно 25 банов» от «банов больше 25» и в последнем случае показать предупреждение.
    ban_entries = [entry async for entry in inter.guild.bans(limit=26)]
    truncated = len(ban_entries) > 25
    ban_entries = ban_entries[:25]

    if not ban_entries:
        await inter.response.send_message("❌ На сервере сейчас никто не забанен.", ephemeral=True)
        return

    prompt = "Выберите, кого разбанить:" if punishment_type == "unban" else "Выберите, с кого снять перманентный бан:"
    if truncated:
        prompt += (
            "\n⚠️ Показаны первые 25 из более чем 25 забаненных — если нужного нет в списке, "
            "используйте `/unban` или `/unpermban` (поле начнёт подсказывать по мере ввода ника)."
        )
    await inter.response.send_message(prompt, view=BannedUserView(ban_entries, punishment_type), ephemeral=True)


class PunishmentTypeView(disnake.ui.View):
    """Разовый эфемерный визард — открывается командой /punishment request, виден и
    доступен только тому, кто её вызвал, поэтому таймаут здесь не проблема."""

    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(PunishmentTypeSelect())


class PunishmentContextTypeSelect(disnake.ui.StringSelect):
    """Тот же выбор типа наказания, что и PunishmentTypeSelect, но цель уже известна
    (пришла из контекстного меню участника) — модалка откроется сразу без поля «Игрок»."""

    def __init__(self, target: disnake.Member):
        self.target = target
        options = [
            disnake.SelectOption(label=PUNISHMENT_LABELS[t], value=t, emoji=icon(PUNISHMENT_ICONS[t]))
            for t in CONTEXT_TARGET_TYPES
        ]
        super().__init__(placeholder=f"Тип наказания для {target.display_name}"[:150], options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        if not _can_request(inter.author):
            await inter.response.send_message(
                "❌ Подавать заявки на наказание может только соответствующая роль.", ephemeral=True
            )
            return
        if self.values[0] == "unwarn" and not _active_warns(inter.guild_id, self.target.id):
            await inter.response.send_message("❌ У этого участника нет активных предупреждений.", ephemeral=True)
            return
        await inter.response.send_modal(PunishmentRequestModal(self.values[0], target=self.target))


class PunishmentContextTypeView(disnake.ui.View):
    """Открывается из контекстного меню участника — разовая эфемерная вьюха, как и
    PunishmentTypeView, только с уже привязанной целью."""

    def __init__(self, target: disnake.Member):
        super().__init__(timeout=300)
        self.add_item(PunishmentContextTypeSelect(target))


def _build_punishment_panel_embed() -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('shield')} Заявка на наказание",
        (
            "Здесь подаются заявки на бан, разбан, пермбан, снятие пермбана, кик, "
            "предупреждение и снятие предупреждения — решение по ним принимает руководство.\n\n"
            f"{icon_tag('help')} **Как это работает**\n"
            "**1.** Выберите тип наказания в меню ниже.\n"
            "**2.** Заполните анкету по выбранному типу (игрок, срок, причина и т.д.).\n"
            "**3.** Заявка уходит на рассмотрение — о решении придёт личное сообщение.\n\n"
            "Быстрее: правый клик по участнику → **Заявка на наказание** — тег вместо "
            "ручного ввода ID/упоминания."
        ),
        panel_key="punishment",
    )
    embed.add_field(
        name=f"{icon_tag('clipboard')} Доступные типы",
        value="\n".join(f"{icon_tag(PUNISHMENT_ICONS[t])} {PUNISHMENT_LABELS[t]}" for t in ALL_TYPES),
        inline=True,
    )
    embed.add_field(
        name=f"{icon_tag('alert')} Ограничение",
        value="Подать заявку может только определённая роль модерации.",
        inline=True,
    )
    return embed


class PunishmentPanelView(disnake.ui.View):
    """Постоянная панель — публикуется один раз командой /punishment show и висит в канале;
    персистентный custom_id у select-меню, регистрируется в PERSISTENT_VIEWS, переживает рестарт."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PunishmentTypeSelect(custom_id="punish_panel_type_select"))


# ---------------------------------------------------------------------------
# Рассмотрение заявки (для moderation.leader_role_ids) — персистентная панель
# в канале заявок, статус определяется по message_id, а не по данным во View.
# ---------------------------------------------------------------------------

class RejectReasonModal(disnake.ui.Modal):
    def __init__(self, request_id: int):
        self.request_id = request_id
        components = [
            disnake.ui.TextInput(
                label="Причина отклонения",
                custom_id="reason",
                style=disnake.TextInputStyle.paragraph,
                max_length=400,
            ),
        ]
        super().__init__(title="Отклонить заявку", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        data = moderation_store.load()
        request = data["requests"].get(str(self.request_id))
        if request is None or request.get("status") != "pending":
            await inter.response.send_message("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        reject_reason = inter.text_values["reason"].strip()
        request["status"] = "rejected"
        request["decided_by"] = inter.author.id
        request["reject_reason"] = reject_reason
        moderation_store.save(data)

        if inter.message is not None:
            embed = inter.message.embeds[0]
            _replace_status_field(embed, f"{icon_tag('cross')} Статус", f"Отклонено — {inter.author.mention}\n{reject_reason}")
            await inter.response.edit_message(embed=embed, view=None, delete_after=REQUEST_CARD_LIFETIME)
        else:
            await inter.response.send_message("✅ Заявка отклонена.", ephemeral=True)

        target_mention = f"<@{request['target_id']}>"
        try:
            target_user = await inter.bot.fetch_user(request["target_id"])
            target_display = str(target_user)
            avatar_url = target_user.display_avatar.url
        except disnake.HTTPException:
            target_display = f"ID {request['target_id']}"
            avatar_url = None

        reject_embed = base_embed(f"{icon_tag('cross')} Заявка #{self.request_id} отклонена", color=disnake.Color.red(), timestamp=True)
        if avatar_url:
            reject_embed.set_thumbnail(url=avatar_url)
        reject_embed.add_field(name=f"{icon_tag('clipboard')} Тип", value=PUNISHMENT_LABELS[request["type"]], inline=True)
        reject_embed.add_field(name=f"{icon_tag('users')} Игрок", value=f"{target_mention}\n`{target_display}`", inline=True)
        if request.get("duration_minutes"):
            reject_embed.add_field(
                name="Запрошенный срок", value=_format_duration(dt.timedelta(minutes=request["duration_minutes"])), inline=True
            )
        reject_embed.add_field(name="Запросил", value=f"<@{request['requested_by']}>", inline=True)
        reject_embed.add_field(name=f"{icon_tag('shield')} Отклонил", value=inter.author.mention, inline=True)
        reject_embed.add_field(name=f"{icon_tag('help')} Причина заявки", value=request["reason"][:1024], inline=False)
        if request.get("evidence"):
            reject_embed.add_field(name="🔍 Доказательства", value=request["evidence"][:1024], inline=False)
        reject_embed.add_field(name=f"{icon_tag('cross')} Причина отклонения", value=reject_reason[:1024], inline=False)
        await _log_moderation(inter.guild, reject_embed)

        requester = inter.guild.get_member(request["requested_by"])
        if requester is not None:
            try:
                dm_embed = base_embed(
                    f"{icon_tag('cross')} Ваша заявка на наказание отклонена",
                    f"Тип: **{PUNISHMENT_LABELS[request['type']]}**\nИгрок: {target_mention}",
                    color=disnake.Color.red(),
                )
                dm_embed.add_field(name="Причина отклонения", value=reject_reason, inline=False)
                await requester.send(embed=dm_embed)
            except disnake.Forbidden:
                pass


class PunishmentReviewView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Одобрить", style=disnake.ButtonStyle.success, emoji=icon("check"), custom_id="punish_approve")
    async def approve(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Решать по заявкам может только руководство.", ephemeral=True)
            return

        request, request_id = _find_request_by_message(inter.message.id)
        if request is None or request.get("status") != "pending":
            await inter.response.send_message("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        # Заявка помечается решённой сразу и синхронно (без await между проверкой и записью) —
        # если два руководителя нажмут одновременно, второй увидит status != "pending" и выйдет,
        # без повторного исполнения наказания.
        data = moderation_store.load()
        data["requests"][str(request_id)]["status"] = "approved"
        data["requests"][str(request_id)]["decided_by"] = inter.author.id
        moderation_store.save(data)

        await inter.response.defer()

        ok, error = await _execute_request(inter, request)

        data = moderation_store.load()
        data["requests"][str(request_id)]["status"] = "approved" if ok else "failed"
        if error:
            data["requests"][str(request_id)]["error"] = error
        moderation_store.save(data)

        embed = inter.message.embeds[0]
        if ok:
            _replace_status_field(embed, f"{icon_tag('check')} Статус", f"Одобрено — {inter.author.mention}")
        else:
            _replace_status_field(embed, f"{icon_tag('alert')} Статус", f"Одобрено {inter.author.mention}, но не удалось выполнить: {error}")
        await inter.edit_original_message(embed=embed, view=None, delete_after=REQUEST_CARD_LIFETIME)

        requester = inter.guild.get_member(request["requested_by"])
        if requester is not None and ok:
            try:
                dm_embed = base_embed(
                    f"{icon_tag('check')} Ваша заявка на наказание одобрена",
                    f"Тип: **{PUNISHMENT_LABELS[request['type']]}**\nИгрок: <@{request['target_id']}>\nНаказание выдано.",
                    color=disnake.Color.green(),
                )
                await requester.send(embed=dm_embed)
            except disnake.Forbidden:
                pass

    @disnake.ui.button(label="Отклонить", style=disnake.ButtonStyle.danger, emoji=icon("cross"), custom_id="punish_reject")
    async def reject(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Решать по заявкам может только руководство.", ephemeral=True)
            return

        request, request_id = _find_request_by_message(inter.message.id)
        if request is None or request.get("status") != "pending":
            await inter.response.send_message("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        await inter.response.send_modal(RejectReasonModal(request_id))


# ---------------------------------------------------------------------------
# Карточка модерации участника — быстрый обзор бана/предупреждений с кнопками для
# немедленного снятия, чтобы не искать причину по разным командам и каналам логов.
# ---------------------------------------------------------------------------

class ModCaseUnbanButton(disnake.ui.Button):
    def __init__(self, target: disnake.User, *, permanent: bool):
        super().__init__(label="Разбанить", style=disnake.ButtonStyle.success, emoji=icon("check"))
        self.target = target
        self.permanent = permanent

    async def callback(self, inter: disnake.MessageInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        ok, error, dm_ok = await _do_unban(
            inter.guild, self.target, "Снято через карточку модерации", inter.author, permanent_label=self.permanent
        )
        if not ok:
            await inter.followup.send(f"❌ Не удалось разбанить: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {self.target.mention} разбанен(а). {_dm_note(dm_ok)}", ephemeral=True)
        self.disabled = True
        if inter.message is not None:
            try:
                await inter.message.edit(view=self.view)
            except disnake.HTTPException:
                pass


class ModCaseActionsView(disnake.ui.View):
    def __init__(self, target: disnake.User, *, banned: bool, permanent: bool, warns: list[dict]):
        super().__init__(timeout=180)
        if banned:
            self.add_item(ModCaseUnbanButton(target, permanent=permanent))
        if warns:
            self.add_item(UnwarnSelect(target, warns))


async def _build_modcase(guild: disnake.Guild, user: disnake.abc.User) -> tuple[disnake.Embed, disnake.ui.View | None]:
    embed = base_embed(f"{icon_tag('clipboard')} Карточка модерации: {user}", color=disnake.Color.dark_grey())
    embed.set_thumbnail(url=user.display_avatar.url)

    try:
        ban_entry = await guild.fetch_ban(user)
    except disnake.HTTPException:
        ban_entry = None

    permanent = False
    if ban_entry is not None:
        data = moderation_store.load()
        temp = data["temp_bans"].get(str(user.id))
        if temp:
            until = dt.datetime.fromisoformat(temp["unban_at"])
            ban_value = (
                f"Временный, до <t:{int(until.timestamp())}:f> (<t:{int(until.timestamp())}:R>)\n"
                f"Причина: {temp.get('reason') or '—'}\nВыдал: <@{temp.get('banned_by')}>"
            )
        else:
            permanent = True
            ban_value = f"Перманентный\nПричина: {ban_entry.reason or '—'}"
        embed.add_field(name=f"{icon_tag('ban')} Статус бана", value=ban_value, inline=False)
    else:
        embed.add_field(name=f"{icon_tag('check')} Статус бана", value="Не забанен", inline=False)

    warns = _active_warns(guild.id, user.id)
    if warns:
        lines = [f"#{w['id']} — {w['reason'][:80]} (<@{w['moderator_id']}>)" for w in warns[:10]]
        embed.add_field(
            name=f"{icon_tag('alert')} Активные предупреждения ({len(warns)})", value="\n".join(lines), inline=False
        )
    else:
        embed.add_field(name=f"{icon_tag('alert')} Активные предупреждения", value="Нет", inline=False)

    view = None
    if ban_entry is not None or warns:
        view = ModCaseActionsView(user, banned=ban_entry is not None, permanent=permanent, warns=warns)
    return embed, view


# ---------------------------------------------------------------------------
# Слэш-команды
# ---------------------------------------------------------------------------

class Moderation(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.check_expired_bans.start()

    def cog_unload(self):
        self.check_expired_bans.cancel()

    @commands.slash_command(name="ban", description="Забанить на время (только для руководства)")
    async def ban(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Кого забанить"),
        days: int = commands.Param(description="Срок бана в днях", min_value=1, max_value=365),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        delta = dt.timedelta(days=days)

        await inter.response.defer(ephemeral=True)
        ok, error, dm_ok = await _do_ban(inter.guild, user, delta, reason, inter.author)
        if not ok:
            await inter.followup.send(f"❌ Не удалось забанить: {error}", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ {user.mention} забанен(а) на {_format_duration(delta)}. {_dm_note(dm_ok)}", ephemeral=True
        )

    @commands.slash_command(name="permban", description="Забанить навсегда (только для руководства)")
    async def permban(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Кого забанить"),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        ok, error, dm_ok = await _do_ban(inter.guild, user, None, reason, inter.author)
        if not ok:
            await inter.followup.send(f"❌ Не удалось забанить: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {user.mention} забанен(а) навсегда. {_dm_note(dm_ok)}", ephemeral=True)

    @commands.slash_command(name="unban", description="Снять бан (только для руководства)")
    async def unban(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user_id: str = commands.Param(
            description="Начните вводить ник — подскажет из уже забаненных", autocomplete=_banned_user_autocomplete
        ),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        if not user_id.isdigit():
            await inter.response.send_message("❌ Выберите пользователя из подсказки автозаполнения.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            user = await inter.bot.fetch_user(int(user_id))
        except disnake.NotFound:
            await inter.followup.send("❌ Пользователь не найден в Discord.", ephemeral=True)
            return
        ok, error, dm_ok = await _do_unban(inter.guild, user, reason, inter.author, permanent_label=False)
        if not ok:
            await inter.followup.send(f"❌ Не удалось разбанить: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {user.mention} разбанен(а). {_dm_note(dm_ok)}", ephemeral=True)

    @commands.slash_command(name="unpermban", description="Снять перманентный бан (только для руководства)")
    async def unpermban(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user_id: str = commands.Param(
            description="Начните вводить ник — подскажет из уже забаненных", autocomplete=_banned_user_autocomplete
        ),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        if not user_id.isdigit():
            await inter.response.send_message("❌ Выберите пользователя из подсказки автозаполнения.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            user = await inter.bot.fetch_user(int(user_id))
        except disnake.NotFound:
            await inter.followup.send("❌ Пользователь не найден в Discord.", ephemeral=True)
            return
        ok, error, dm_ok = await _do_unban(inter.guild, user, reason, inter.author, permanent_label=True)
        if not ok:
            await inter.followup.send(f"❌ Не удалось разбанить: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {user.mention} разбанен(а). {_dm_note(dm_ok)}", ephemeral=True)

    @commands.slash_command(name="kick", description="Кикнуть с сервера (только для руководства)")
    async def kick(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Кого кикнуть"),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        ok, error = await _do_kick(member, reason, inter.author)
        if not ok:
            await inter.followup.send(f"❌ Не удалось кикнуть: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {member.mention} кикнут(а).", ephemeral=True)

    @commands.slash_command(name="mute", description="Замутить на время (роль high и выше)")
    async def mute(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Кого замутить"),
        duration: str = commands.Param(description="Срок — например 1d, 12h, 45m (максимум 28 дней)"),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _can_mute(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        delta = _parse_duration(duration)
        if delta is None or delta > MAX_MUTE_DURATION:
            await inter.response.send_message(
                f"❌ Некорректный срок. {DURATION_HELP} Максимум для мута — 28 дней (ограничение Discord).", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        ok, error = await _do_mute(member, delta, reason, inter.author)
        if not ok:
            await inter.followup.send(f"❌ Не удалось замутить: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ {member.mention} замучен(а) на {_format_duration(delta)}.", ephemeral=True)

    @commands.slash_command(name="unmute", description="Снять мут (роль high и выше)")
    async def unmute(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="С кого снять мут"),
        reason: str = commands.Param(description="Причина", max_length=400),
    ):
        if not _can_mute(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        ok, error = await _do_unmute(member, reason, inter.author)
        if not ok:
            await inter.followup.send(f"❌ Не удалось снять мут: {error}", ephemeral=True)
            return
        await inter.followup.send(f"✅ Мут снят с {member.mention}.", ephemeral=True)

    @commands.slash_command(name="punishment", description="Заявка на наказание")
    async def punishment(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @punishment.sub_command(name="request", description="Подать заявку на бан/разбан/кик (только для роли high)")
    async def punishment_request(self, inter: disnake.ApplicationCommandInteraction):
        if not _can_request(inter.author):
            await inter.response.send_message("❌ Недостаточно прав для подачи заявки на наказание.", ephemeral=True)
            return
        await inter.response.send_message(
            "Выберите тип наказания:", view=PunishmentTypeView(), ephemeral=True
        )

    @punishment.sub_command(name="show", description="Опубликовать постоянную панель заявки на наказание (только для руководства)")
    async def punishment_show(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await send_panel(inter, _build_punishment_panel_embed(), view=PunishmentPanelView(), panel_key="punishment")

    @commands.user_command(name="Заявка на наказание")
    async def punishment_request_context(self, inter: disnake.UserCommandInteraction):
        """Быстрый путь: правый клик по участнику → сразу выбор типа наказания, без
        ручного набора упоминания/ID в анкете."""
        if not _can_request(inter.author):
            await inter.response.send_message("❌ Недостаточно прав для подачи заявки на наказание.", ephemeral=True)
            return
        target = inter.target
        if not isinstance(target, disnake.Member):
            await inter.response.send_message("❌ Этот пользователь не является участником сервера.", ephemeral=True)
            return
        if target.id == inter.author.id:
            await inter.response.send_message("❌ Нельзя подать заявку на самого себя.", ephemeral=True)
            return
        if target.bot:
            await inter.response.send_message("❌ Нельзя подать заявку на бота.", ephemeral=True)
            return
        await inter.response.send_message(
            f"Выберите тип наказания для {target.mention}:",
            view=PunishmentContextTypeView(target),
            ephemeral=True,
        )

    @commands.slash_command(name="modcase", description="Карточка модерации участника: баны и предупреждения (только для руководства)")
    async def modcase(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Чья карточка"),
    ):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        embed, view = await _build_modcase(inter.guild, user)
        await inter.followup.send(embed=embed, view=view, ephemeral=True)

    @commands.user_command(name="Карточка модерации")
    async def modcase_context(self, inter: disnake.UserCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        embed, view = await _build_modcase(inter.guild, inter.target)
        await inter.followup.send(embed=embed, view=view, ephemeral=True)

    @commands.slash_command(name="banlist", description="Список забаненных участников сервера (только для руководства)")
    async def banlist(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)

        data = moderation_store.load()
        temp_bans = data["temp_bans"]
        temp_lines, perm_lines = [], []
        async for ban_entry in inter.guild.bans(limit=1000):
            user = ban_entry.user
            temp = temp_bans.get(str(user.id))
            if temp:
                until = dt.datetime.fromisoformat(temp["unban_at"])
                temp_lines.append(f"{user.mention} `{user}` — до <t:{int(until.timestamp())}:R>")
            else:
                perm_lines.append(f"{user.mention} `{user}` — {ban_entry.reason or 'без причины в бан-листе'}")

        if not temp_lines and not perm_lines:
            await inter.followup.send("✅ Забаненных участников нет.", ephemeral=True)
            return

        embed = base_embed(
            f"{icon_tag('ban')} Забаненные участники ({len(temp_lines) + len(perm_lines)})",
            color=disnake.Color.dark_grey(),
        )
        if temp_lines:
            embed.add_field(
                name=f"{icon_tag('gavel')} Временный бан ({len(temp_lines)})",
                value="\n".join(temp_lines[:25]) + (f"\n…и ещё {len(temp_lines) - 25}" if len(temp_lines) > 25 else ""),
                inline=False,
            )
        if perm_lines:
            embed.add_field(
                name=f"{icon_tag('ban')} Перманентный бан ({len(perm_lines)})",
                value="\n".join(perm_lines[:25]) + (f"\n…и ещё {len(perm_lines) - 25}" if len(perm_lines) > 25 else ""),
                inline=False,
            )
        await inter.followup.send(embed=embed, ephemeral=True)

    @commands.slash_command(name="warnlist", description="Список участников с активными предупреждениями (только для руководства)")
    async def warnlist(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return

        data = warns_store.load()
        active = [w for w in data["warns"] if w["guild_id"] == inter.guild_id and w["active"]]
        if not active:
            await inter.response.send_message("✅ Активных предупреждений нет.", ephemeral=True)
            return

        by_user: dict[int, list[dict]] = {}
        for w in active:
            by_user.setdefault(w["user_id"], []).append(w)
        threshold = config.get("warns.threshold", 3)

        lines = []
        for uid, warns in sorted(by_user.items(), key=lambda kv: -len(kv[1]))[:40]:
            mark = f" {icon_tag('alert')}" if len(warns) >= threshold else ""
            lines.append(f"<@{uid}> — **{len(warns)}**{mark}")

        embed = base_embed(
            f"{icon_tag('alert')} Участники с предупреждениями ({len(by_user)})",
            "\n".join(lines),
            color=disnake.Color.orange(),
        )
        if len(by_user) > 40:
            embed.set_footer(text=f"Показаны первые 40 из {len(by_user)}.")
        await inter.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(minutes=EXPIRY_CHECK_MINUTES)
    async def check_expired_bans(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return

        data = moderation_store.load()
        now = dt.datetime.now(dt.timezone.utc)
        due = [
            (uid, entry) for uid, entry in data["temp_bans"].items()
            if dt.datetime.fromisoformat(entry["unban_at"]) <= now
        ]
        if not due:
            return

        changed = False
        for uid, entry in due:
            try:
                await guild.unban(disnake.Object(id=int(uid)), reason="Истёк срок временного бана")
            except disnake.NotFound:
                pass  # уже разбанен вручную
            except disnake.HTTPException:
                continue  # попробуем на следующем цикле

            data["temp_bans"].pop(uid, None)
            changed = True
            try:
                target = await self.bot.fetch_user(int(uid))
            except disnake.HTTPException:
                target = disnake.Object(id=int(uid))

            dm_embed = base_embed(
                f"{icon_tag('check')} Ваш временный бан истёк",
                f"На сервере семьи **{guild.name}** истёк срок вашего временного бана — доступ восстановлен.",
                color=disnake.Color.green(),
            )
            await _notify_target(target, dm_embed)

            await _log_moderation(
                guild,
                _build_action_log_embed(
                    "⏰ Автоматический разбан (истёк срок)",
                    target, "🤖 Автоматически", entry.get("reason", "—"),
                ),
            )

        if changed:
            moderation_store.save(data)

    @check_expired_bans.before_loop
    async def _before_check_expired_bans(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.InteractionBot):
    bot.add_cog(Moderation(bot))


PERSISTENT_VIEWS = [lambda: PunishmentReviewView(), lambda: PunishmentPanelView()]
