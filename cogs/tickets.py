from __future__ import annotations

import asyncio
import datetime as dt
import io

import disnake
from disnake.ext import commands

from core.branding import FAMILY_NAME, base_embed, panel_file_kwargs, send_panel
from core.checks import is_staff
from core.config import config, log_channel_id
from core.icons import icon, icon_tag
from core.storage import tickets_store

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

PANELS = {
    "curator": {
        "title": "Тикеты кураторов академии",
        "icon": "graduation",
        "description": (
            "Если у вас возникли вопросы по обучению в академии или вам требуется помощь куратора, "
            "нажмите кнопку ниже, чтобы открыть тикет."
        ),
        "button_label": "Обратиться к куратору",
        "modal_prompt": "Кратко опишите ваш вопрос к куратору",
    },
    "recruit": {
        "title": "Заявки в семью RESTRUCT",
        "icon": "clipboard",
        "description": (
            "**Путь в семью начинается здесь.**\n"
            "Мы ценим каждого, кто хочет присоединиться к RESTRUCT — поэтому подходим "
            "к отбору внимательно и без спешки.\n\n"
            f"{icon_tag('send')} **Как подать заявку**\n"
            "Выберите тип заявки в меню под этим сообщением и подробно заполните анкету. "
            "Чем содержательнее ответы — тем быстрее и увереннее будет решение.\n\n"
            f"{icon_tag('pending')} **Сроки**\n"
            "Обычно заявки рассматриваются в течение 24 часов. С вами свяжется представитель "
            "отдела рекрутинга в созданном для вас приватном канале.\n\n"
            f"{icon_tag('alert')} **Важно**\n"
            "Подавайте заявку только при открытом наборе и указывайте только достоверную "
            "информацию — это напрямую влияет на решение."
        ),
    },
    "general": {
        "title": "Служба поддержки семьи",
        "icon": "ticket",
        "description": (
            "Основная панель для создания тикетов. Выберите категорию обращения в меню ниже, "
            "и с вами свяжется ответственный сотрудник."
        ),
        "button_label": "Открыть тикет",
        "modal_prompt": "Опишите свой вопрос или проблему",
    },
    "shop": {
        "title": "Магазин услуг",
        "icon": "package",
        "description": "Заказ услуг семьи RESTRUCT — тикет создаётся отдельным магазинным каталогом (/shop).",
    },
}

GENERAL_CATEGORIES = {
    "question": ("Общий вопрос", "help"),
    "complaint": ("Жалоба на игрока/сотрудника", "alert"),
    "technical": ("Техническая проблема", "tool"),
    "other": ("Другое", "package"),
}

RECRUIT_OPTIONS = {
    "family": "Заявка в семью",
    "vzp": "Заявка в ВЗП",
}


def _ticket_config(key: str) -> dict:
    return config.get(f"tickets.{key}", {}) or {}


def _staff_role_ids(ticket_type: str) -> list[int]:
    cfg = _ticket_config(ticket_type)
    ids = [cfg.get("role_id"), *(cfg.get("extra_role_ids", []) or [])]
    return [i for i in ids if i]


def _disable_buttons(view: disnake.ui.View, custom_ids: tuple[str, ...]) -> None:
    for child in view.children:
        if isinstance(child, disnake.ui.Button) and child.custom_id in custom_ids:
            child.disabled = True


async def _add_recruit_progress_role(guild: disnake.Guild, opener: disnake.Member | None) -> str | None:
    """Выдаёт заявителю роль «заявка в обработке» в момент, когда сотрудник забирает его
    заявку. Возвращает текст предупреждения (для эфемерного ответа сотруднику), если роль
    выдать не удалось — сам процесс взятия заявки в работу при этом не прерывается."""
    role_id = _ticket_config("recruit").get("in_progress_role_id")
    if opener is None or not role_id:
        return None
    role = guild.get_role(role_id)
    if role is None:
        return "⚠️ Роль «заявка в обработке» не найдена на сервере (проверьте in_progress_role_id в config.json)."
    try:
        await opener.add_roles(role, reason="Заявка взята в обработку")
    except disnake.Forbidden:
        return (
            f"⚠️ Не удалось выдать роль {role.mention} — роль бота ниже неё в иерархии "
            "ролей сервера. Поднимите роль бота выше в Настройки → Роли."
        )
    return None


async def _remove_recruit_progress_role(guild: disnake.Guild, opener: disnake.Member | None) -> None:
    """Снимает роль «заявка в обработке» после того, как по заявке принято решение
    (принята или отклонена)."""
    role_id = _ticket_config("recruit").get("in_progress_role_id")
    if opener is None or not role_id:
        return
    role = guild.get_role(role_id)
    if role is None:
        return
    try:
        await opener.remove_roles(role, reason="Решение по заявке принято")
    except disnake.Forbidden:
        pass


async def _post_decision(
    channel: disnake.TextChannel, info: dict, opener: disnake.Member | None, embed: disnake.Embed
) -> None:
    """Редактирует сообщение «взята в обработку» на итоговое решение, либо отправляет новое,
    если по какой-то причине его не нашлось."""
    processing_message_id = info.get("processing_message_id")
    if processing_message_id:
        try:
            processing_message = await channel.fetch_message(processing_message_id)
            await processing_message.edit(content=opener.mention if opener is not None else None, embed=embed)
            return
        except disnake.HTTPException:
            pass
    await channel.send(content=opener.mention if opener is not None else None, embed=embed)


# Короткие однострочные ответы анкеты — рядом друг с другом (inline), не отдельным
# полем на всю ширину; всё остальное (абзацы про опыт и т.п.) — full-width.
_SHORT_ANSWER_LABELS = {
    "Имя, возраст и игровой ник",
    "LVL, онлайн и часовой пояс",
    "Никнейм персонажа",
    "Номер паспорта",
}


async def _post_database_entry(
    guild: disnake.Guild, info: dict, opener: disnake.Member | None, accepted_by: disnake.Member
) -> None:
    """Пишет принятую заявку (семья/ВЗП) отдельной карточкой в channels.database — полная
    анкета из тикета плюс Discord заявителя. ВЗП и обычные заявки визуально различаются
    цветом и меткой типа, чтобы записи легко читались вперемешку в одном канале."""
    channel_id = config.get("channels.database")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    is_vzp = info.get("subtype") == "vzp"
    title = f"{icon_tag('users')} Новый сотрудник ВЗП" if is_vzp else f"{icon_tag('users')} Новый участник семьи"
    color = disnake.Color.gold() if is_vzp else disnake.Color.green()

    embed = base_embed(title, color=color, timestamp=True)
    if opener is not None:
        embed.set_thumbnail(url=opener.display_avatar.url)
    if guild.banner is not None:
        embed.set_image(url=guild.banner.url)

    embed.add_field(
        name=f"{icon_tag('send')} Discord",
        value=f"{opener.mention}\n`{opener}`" if opener is not None else f"<@{info['opener_id']}>",
        inline=True,
    )
    embed.add_field(name=f"{icon_tag('clipboard')} Тип", value="🎯 ВЗП" if is_vzp else "🏠 Семья", inline=True)
    embed.add_field(name=f"{icon_tag('check')} Принял", value=accepted_by.mention, inline=True)

    answers = info.get("answers", [])
    if answers:
        embed.add_field(name="​", value=f"{icon_tag('clipboard')} **Анкета заявителя**", inline=False)
        for field_name, field_value in answers:
            embed.add_field(
                name=field_name,
                value=(field_value or "—")[:1024],
                inline=field_name in _SHORT_ANSWER_LABELS,
            )

    embed.set_footer(text=f"Заявка #{info.get('number', '?')} • Семья {FAMILY_NAME}")

    try:
        await channel.send(embed=embed)
    except disnake.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Список активных заявок (recruit) в канале модерации
# ---------------------------------------------------------------------------

def _recruit_label(subtype: str | None) -> str:
    text = RECRUIT_OPTIONS.get(subtype, "Заявка")
    return f"{icon_tag('send')} {text}"


def _active_ticket_label(info: dict) -> str:
    return _recruit_label(info.get("subtype"))


def _active_tickets() -> list[tuple[str, dict]]:
    data = tickets_store.load()
    active = [(cid, info) for cid, info in data["open"].items() if info.get("type") == "recruit"]
    active.sort(key=lambda kv: kv[1].get("number", 0))
    return active


def _build_active_tickets_embed(active: list[tuple[str, dict]]) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('clipboard')} Активные заявки",
        "Центр обработки заявок в семью и на ВЗП. Этот список обновляется автоматически — "
        "ничего не нужно делать вручную.",
        panel_key="moderation",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            "**1.** Выберите заявку в меню под этим сообщением — откроется рабочая панель, "
            "видимая только вам.\n"
            f"**2.** Нажмите {icon_tag('hand')} **Забрать** — без этого решение принять нельзя.\n"
            f"**3.** Отметьте {icon_tag('check')} **Принять** или {icon_tag('cross')} **Отклонить**.\n"
            f"**4.** Нажмите {icon_tag('lock')} **Закрыть** — рабочая панель исчезнет, "
            "а этот список обновится сам."
        ),
        inline=False,
    )

    MAX_LISTED = 10
    if not active:
        embed.add_field(name=f"{icon_tag('clipboard')} Заявки", value="Сейчас нет открытых заявок.", inline=False)
        return embed

    status_icon = {"accepted": "check", "rejected": "cross"}
    status_word = {"accepted": "принята", "rejected": "отклонена"}
    lines = []
    for channel_id, info in active[:MAX_LISTED]:
        label = _active_ticket_label(info)
        decision = info.get("decision")
        if decision in status_icon:
            status = f"{icon_tag(status_icon[decision])} **{status_word[decision]}**"
        elif info.get("claimed_by"):
            status = f"{icon_tag('pending')} в работе — <@{info['claimed_by']}>"
        else:
            status = f"{icon_tag('unassigned')} не назначена"
        lines.append(
            f"`#{info.get('number')}` **{label}**\n"
            f"┗ <#{channel_id}> · <@{info['opener_id']}> · {status}"
        )
    value = "\n\n".join(lines)
    if len(active) > MAX_LISTED:
        value += f"\n\n*...и ещё {len(active) - MAX_LISTED}*"
    embed.add_field(name=f"{icon_tag('clipboard')} Заявки ({len(active)})", value=value[:1024], inline=False)
    return embed


def _build_work_embed(channel: disnake.TextChannel, info: dict) -> disnake.Embed:
    label = _active_ticket_label(info)
    embed = base_embed(
        f"{icon_tag('clipboard')} Заявка `#{info.get('number')}` — {label}",
        panel_key="moderation",
    )
    embed.add_field(name="Автор", value=f"<@{info['opener_id']}>", inline=True)
    embed.add_field(name="Тикет", value=channel.mention, inline=True)
    claimed_by = info.get("claimed_by")
    embed.add_field(
        name="Ответственный",
        value=f"<@{claimed_by}>" if claimed_by else f"{icon_tag('unassigned')} не назначен",
        inline=True,
    )
    decision = info.get("decision")
    if decision:
        embed.add_field(
            name="Решение",
            value=f"{icon_tag('check')} Принята" if decision == "accepted" else f"{icon_tag('cross')} Отклонена",
            inline=True,
        )

    claim_hint = "" if claimed_by else " _(сначала заберите заявку)_"
    embed.add_field(
        name=f"{icon_tag('settings')} Кнопки",
        value=(
            f"{icon_tag('hand')} — **Забрать** заявку в обработку\n"
            f"{icon_tag('shield')} — Ответить **от администрации** (анонимно)\n"
            f"{icon_tag('check')} — **Принять** заявку{claim_hint}\n"
            f"{icon_tag('cross')} — **Отклонить** заявку (с указанием причины){claim_hint}\n"
            f"{icon_tag('lock')} — **Закрыть** тикет (доступно после решения)"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Заявка #{info.get('number')} • Семья RESTRUCT")
    return embed


def _build_active_tickets_view() -> "ActiveTicketsView":
    return ActiveTicketsView(_active_tickets())


def _prune_stale_tickets(guild: disnake.Guild) -> list[tuple[str, dict]]:
    """Убирает из хранилища заявки, чей канал был удалён в обход бота (вручную, через
    настройки сервера) — иначе они вечно висели бы в списке как «# неизвестно»."""
    data = tickets_store.load()
    stale_ids = [
        cid
        for cid, info in data["open"].items()
        if info.get("type") == "recruit" and guild.get_channel(int(cid)) is None
    ]
    if stale_ids:
        for cid in stale_ids:
            data["open"].pop(cid, None)
        tickets_store.save(data)
    return _active_tickets()


async def _refresh_active_tickets_list(guild: disnake.Guild) -> None:
    """Поддерживает единственное сообщение со списком активных заявок в канале модерации
    в актуальном состоянии: создаёт при первой необходимости, дальше только редактирует.
    Канал берётся из того, что задали командой /moderation, а если её ни разу не
    вызывали — из старого статического moderation_channel_id в config.json."""
    data = tickets_store.load()
    mod_channel_id = data.get("active_list_channel_id") or _ticket_config("recruit").get("moderation_channel_id")
    mod_channel = guild.get_channel(mod_channel_id) if mod_channel_id else None
    if mod_channel is None:
        return

    active = _prune_stale_tickets(guild)
    embed = _build_active_tickets_embed(active)
    view = ActiveTicketsView(active)

    list_message_id = data.get("active_list_message_id")
    message = None
    if list_message_id:
        try:
            message = await mod_channel.fetch_message(list_message_id)
            await message.edit(embed=embed, view=view)
        except disnake.HTTPException:
            message = None
    if message is None:
        message = await mod_channel.send(embed=embed, view=view, **panel_file_kwargs("moderation"))
        data["active_list_message_id"] = message.id
        tickets_store.save(data)


class ActiveTicketSelect(disnake.ui.StringSelect):
    def __init__(self, active: list[tuple[str, dict]]):
        if active:
            options = [
                disnake.SelectOption(
                    label=f"#{info.get('number')} — {RECRUIT_OPTIONS.get(info.get('subtype'), 'Заявка')}"[:100],
                    value=channel_id,
                    description=f"Открыл ID: {info['opener_id']}"[:100],
                    emoji=icon("send"),
                )
                for channel_id, info in active[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет активных заявок", value="none")]
        super().__init__(
            placeholder="Выберите заявку, чтобы начать работу",
            options=options,
            custom_id="active_tickets_select",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет активных заявок.", ephemeral=True)
            return

        channel_id = self.values[0]
        data = tickets_store.load()
        info = data["open"].get(channel_id)
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            await _refresh_active_tickets_list(inter.guild)
            return

        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message("❌ Только сотрудники могут работать с заявками.", ephemeral=True)
            return

        channel = inter.guild.get_channel(int(channel_id))
        if channel is None:
            await inter.response.send_message(
                "❌ Канал этой заявки был удалён вручную — убрал её из списка.", ephemeral=True
            )
            await _refresh_active_tickets_list(inter.guild)
            return

        embed = _build_work_embed(channel, info)
        await inter.response.send_message(embed=embed, view=TicketWorkView(channel), ephemeral=True)


class ActiveTicketsView(disnake.ui.View):
    def __init__(self, active: list[tuple[str, dict]]):
        super().__init__(timeout=None)
        self.add_item(ActiveTicketSelect(active))


# ---------------------------------------------------------------------------
# Создание тикет-каналов
# ---------------------------------------------------------------------------

async def _create_ticket_channel(
    inter: disnake.Interaction,
    ticket_key: str,
    reason_text: str | None,
    category_label: str | None = None,
    extra_fields: list[tuple[str, str]] | None = None,
    name_suffix: str | None = None,
    embed_title: str | None = None,
    external_moderation: bool = False,
    status_note: str | None = None,
) -> disnake.TextChannel | None:
    """Создаёт тикет-канал. Вызывающий обязан заранее сделать inter.response.defer(ephemeral=True) —
    в случае ошибки создания канала используется inter.followup, а не inter.response.

    external_moderation=True — как у recruit: кнопки управления (Забрать/От администрации/
    Закрыть) не публикуются в самом тикете, вся модерация ведётся из отдельного канала —
    вызывающий модуль сам отвечает за реализацию своей рабочей панели там. status_note
    заменяет стандартную подсказку «Кнопки» на произвольный текст (например, куда идти
    за модерацией)."""
    is_recruit = ticket_key == "recruit"
    hide_controls = is_recruit or external_moderation
    cfg = _ticket_config(ticket_key)
    category_id = cfg.get("category_id")
    role_id = cfg.get("role_id")
    extra_role_ids = cfg.get("extra_role_ids", []) or []

    guild = inter.guild
    category = guild.get_channel(category_id) if category_id else None
    role = guild.get_role(role_id) if role_id else None
    extra_roles = [r for r in (guild.get_role(rid) for rid in extra_role_ids) if r is not None]

    data = tickets_store.load()
    data["counter"] += 1
    number = data["counter"]
    base_name = f"{ticket_key}-{name_suffix}" if name_suffix else ticket_key
    channel_name = f"{base_name}-{number:04d}"

    overwrites = {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        inter.author: disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if role is not None:
        overwrites[role] = disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    for extra_role in extra_roles:
        overwrites[extra_role] = disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    try:
        channel = await guild.create_text_channel(
            channel_name,
            category=category if isinstance(category, disnake.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"Тикет открыт пользователем {inter.author} ({inter.author.id})",
        )
    except disnake.Forbidden:
        await inter.followup.send(
            "❌ У бота недостаточно прав для создания тикет-канала. Обратитесь к администратору.",
            ephemeral=True,
        )
        return None

    data["open"][str(channel.id)] = {
        "type": ticket_key,
        "subtype": name_suffix,
        "number": number,
        "opener_id": inter.author.id,
        "claimed_by": None,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "answers": extra_fields or [],
    }
    tickets_store.save(data)

    panel = PANELS[ticket_key]
    title = embed_title or f"{icon_tag(panel['icon'])} Тикет: {panel['title']}"
    embed = base_embed(title)
    embed.add_field(name="Открыл", value=inter.author.mention, inline=True)
    embed.add_field(name="Ответственный", value="Пока не назначен", inline=True)
    if category_label:
        embed.add_field(name="Категория", value=category_label, inline=True)
    if extra_fields:
        for field_name, field_value in extra_fields:
            embed.add_field(name=field_name, value=field_value or "—", inline=False)
    else:
        embed.add_field(name="Описание обращения", value=reason_text or "—", inline=False)

    if is_recruit:
        # Кнопки модерации живут в отдельном канале — заявитель их не видит.
        embed.add_field(
            name=f"{icon_tag('pending')} Статус",
            value="Заявку рассматривает отдел рекрутинга. Ожидайте, с вами свяжутся.",
            inline=False,
        )
    elif external_moderation:
        embed.add_field(
            name=f"{icon_tag('pending')} Статус",
            value=status_note or "Обращение обрабатывается персоналом в отдельном канале модерации.",
            inline=False,
        )
    else:
        embed.add_field(
            name=f"{icon_tag('settings')} Кнопки",
            value=(
                f"{icon_tag('hand')} — **Забрать** тикет в обработку\n"
                f"{icon_tag('shield')} — Ответить **от администрации** (анонимно)\n"
                f"{icon_tag('lock')} — **Закрыть** тикет"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Тикет #{number} • Семья RESTRUCT")

    content_parts = [inter.author.mention]
    if role is not None:
        content_parts.append(role.mention)
    content = " ".join(content_parts)

    # Кнопки модерации для заявок (recruit) и других external_moderation-тикетов живут
    # в отдельном канале, а не в самом тикете — автор их не видит и не должен иметь
    # возможность на них нажать.
    control_view = None if hide_controls else TicketControlView()

    panel_message = await channel.send(
        content=content,
        embed=embed,
        view=control_view,
        allowed_mentions=disnake.AllowedMentions(users=True, roles=True),
    )

    data = tickets_store.load()
    if str(channel.id) in data["open"]:
        data["open"][str(channel.id)]["panel_message_id"] = panel_message.id
        tickets_store.save(data)

    if is_recruit:
        await _refresh_active_tickets_list(guild)

    return channel


class TicketOpenModal(disnake.ui.Modal):
    def __init__(self, ticket_key: str, category_label: str | None = None):
        self.ticket_key = ticket_key
        self.category_label = category_label
        panel = PANELS[ticket_key]
        components = [
            disnake.ui.TextInput(
                label=panel["modal_prompt"][:45],
                custom_id="reason",
                style=disnake.TextInputStyle.paragraph,
                max_length=500,
            ),
        ]
        super().__init__(title=panel["title"][:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        channel = await _create_ticket_channel(
            inter, self.ticket_key, inter.text_values["reason"].strip(), self.category_label
        )
        if channel is not None:
            await inter.followup.send(f"✅ Тикет создан: {channel.mention}", ephemeral=True)


class AdminReplyModal(disnake.ui.Modal):
    def __init__(self, target_channel: disnake.TextChannel | None = None):
        self.target_channel = target_channel
        components = [
            disnake.ui.TextInput(
                label="Текст сообщения от лица администрации",
                custom_id="text",
                style=disnake.TextInputStyle.paragraph,
                max_length=1500,
            ),
        ]
        super().__init__(title="Ответ от администрации", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        text = inter.text_values["text"].strip()
        channel = self.target_channel or inter.channel

        embed = base_embed("🛡️ Сообщение от администрации", text)
        await channel.send(embed=embed)
        await inter.response.send_message("✅ Сообщение отправлено от лица администрации.", ephemeral=True)

        channel_id = log_channel_id("tickets")
        if channel_id:
            log_channel = inter.guild.get_channel(channel_id)
            if log_channel:
                log_embed = base_embed("🛡️ Анонимный ответ в тикете", color=disnake.Color.dark_grey(), timestamp=True)
                log_embed.add_field(name="Канал", value=channel.mention, inline=True)
                log_embed.add_field(name="Реальный автор", value=inter.author.mention, inline=True)
                log_embed.add_field(name="Текст", value=text, inline=False)
                await log_channel.send(embed=log_embed)


class RecruitApplicationModal(disnake.ui.Modal):
    """Анкета заявки — свой набор вопросов на каждый подтип: ВЗП интересует только
    никнейм/паспорт/откат стрельбы, а заявка в семью — общая анкета игрока без отката
    (это не про боевой стиль, а про то, кто вообще к нам приходит)."""

    def __init__(self, subtype: str):
        self.subtype = subtype
        suffix = "в семью" if subtype == "family" else "в ВЗП"

        if subtype == "vzp":
            components = [
                disnake.ui.TextInput(
                    label="Никнейм персонажа",
                    custom_id="nickname",
                    style=disnake.TextInputStyle.short,
                    max_length=60,
                ),
                disnake.ui.TextInput(
                    label="Номер паспорта",
                    custom_id="passport",
                    style=disnake.TextInputStyle.short,
                    max_length=30,
                ),
                disnake.ui.TextInput(
                    label="Откат стрельбы (обязателен)",
                    custom_id="recoil",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=300,
                    placeholder="Например: карабин mk2/тяжёлая винтовка + тяжёлый дробовик",
                ),
            ]
        else:
            components = [
                disnake.ui.TextInput(
                    label="Имя, возраст и игровой ник",
                    custom_id="name_age_nick",
                    style=disnake.TextInputStyle.short,
                    max_length=100,
                    placeholder="Например: Дмитрий, 17, El Twix 765",
                ),
                disnake.ui.TextInput(
                    label="Ваш опыт на RP проектах?",
                    custom_id="rp_experience",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=500,
                ),
                disnake.ui.TextInput(
                    label="Ваш LVL, онлайн и часовой пояс",
                    custom_id="level_online_tz",
                    style=disnake.TextInputStyle.short,
                    max_length=100,
                    placeholder="Например: 10 LVL, 10ч онлайн, (+-1 МСК)",
                ),
                disnake.ui.TextInput(
                    label="Опыт в семьях — где состояли?",
                    custom_id="family_experience",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=500,
                ),
            ]
        super().__init__(title=f"Заявка {suffix}"[:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        label = _recruit_label(self.subtype)

        if self.subtype == "vzp":
            fields = [
                ("Никнейм персонажа", inter.text_values["nickname"].strip()),
                ("Номер паспорта", inter.text_values["passport"].strip()),
                ("Откат стрельбы", inter.text_values["recoil"].strip()),
            ]
        else:
            fields = [
                ("Имя, возраст и игровой ник", inter.text_values["name_age_nick"].strip()),
                ("Опыт на RP проектах", inter.text_values["rp_experience"].strip()),
                ("LVL, онлайн и часовой пояс", inter.text_values["level_online_tz"].strip()),
                ("Опыт в семьях", inter.text_values["family_experience"].strip()),
            ]
        channel = await _create_ticket_channel(
            inter,
            "recruit",
            None,
            category_label=label,
            extra_fields=fields,
            name_suffix=self.subtype,
            embed_title=label,
        )
        if channel is not None:
            await inter.followup.send(f"{icon_tag('check')} Заявка отправлена: {channel.mention}", ephemeral=True)


class RecruitTicketView(disnake.ui.View):
    """Кнопки вместо select-меню намеренно: у Discord есть известный клиентский баг —
    после того как select-меню открывает модалку, оно может «залипнуть» и не реагировать
    на повторный выбор (в том числе того же пункта). У обычных кнопок этой проблемы нет."""

    def __init__(self):
        super().__init__(timeout=None)
        for key, label in RECRUIT_OPTIONS.items():
            self.add_item(self._make_button(key, label))

    @staticmethod
    def _make_button(key: str, label: str) -> disnake.ui.Button:
        button: disnake.ui.Button = disnake.ui.Button(
            label=label,
            emoji=icon("send"),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"ticket_recruit_open_{key}",
        )

        async def callback(inter: disnake.MessageInteraction, key=key):
            await inter.response.send_modal(RecruitApplicationModal(key))

        button.callback = callback
        return button


class GeneralTicketView(disnake.ui.View):
    """См. комментарий в RecruitTicketView — кнопки вместо select-меню, чтобы не ловить
    баг Discord с залипанием после открытия модалки."""

    def __init__(self):
        super().__init__(timeout=None)
        for key, (label, icon_key) in GENERAL_CATEGORIES.items():
            self.add_item(self._make_button(key, label, icon_key))

    @staticmethod
    def _make_button(key: str, label: str, icon_key: str) -> disnake.ui.Button:
        button: disnake.ui.Button = disnake.ui.Button(
            label=label,
            emoji=icon(icon_key),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"ticket_general_open_{key}",
        )

        async def callback(inter: disnake.MessageInteraction, label=label):
            await inter.response.send_modal(TicketOpenModal("general", category_label=label))

        button.callback = callback
        return button


def _make_open_button_view(ticket_key: str) -> disnake.ui.View:
    panel = PANELS[ticket_key]

    class SinglePanelView(disnake.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @disnake.ui.button(
            label=panel["button_label"],
            emoji=icon(panel["icon"]),
            style=disnake.ButtonStyle.primary,
            custom_id=f"ticket_open_{ticket_key}",
        )
        async def open_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
            await inter.response.send_modal(TicketOpenModal(ticket_key))

    return SinglePanelView()


class TicketControlView(disnake.ui.View):
    """Кнопки прямо в канале тикета. Используется для curator/general — там весь персонал,
    кто видит канал, и так должен иметь доступ к управлению. Для заявок (recruit) не
    используется — там модерация вынесена в отдельный канал через список активных заявок
    (см. ActiveTicketsView / TicketWorkView)."""

    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Забрать тикет", emoji=icon("hand"), style=disnake.ButtonStyle.secondary, custom_id="ticket_claim", row=0)
    async def claim_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        data = tickets_store.load()
        info = data["open"].get(str(inter.channel.id))
        if info is None:
            await inter.response.send_message("❌ Этот тикет не найден в базе данных.", ephemeral=True)
            return

        if not is_staff(inter.author, *_staff_role_ids(info["type"])):
            await inter.response.send_message("❌ Только сотрудники могут забирать тикеты.", ephemeral=True)
            return

        claimed_by = info.get("claimed_by")
        if claimed_by is not None:
            await inter.response.send_message(f"❌ Тикет уже забрал <@{claimed_by}>.", ephemeral=True)
            return

        info["claimed_by"] = inter.author.id
        tickets_store.save(data)

        embed = inter.message.embeds[0]
        for i, field in enumerate(embed.fields):
            if field.name == "Ответственный":
                embed.set_field_at(i, name="Ответственный", value=inter.author.mention, inline=True)
                break

        button.disabled = True
        button.label = "Тикет забран"
        await inter.response.edit_message(embed=embed, view=self)
        await inter.followup.send("✅ Вы забрали тикет.", ephemeral=True)

    @disnake.ui.button(label="От администрации", emoji=icon("shield"), style=disnake.ButtonStyle.secondary, custom_id="ticket_admin_reply", row=1)
    async def admin_reply_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        data = tickets_store.load()
        info = data["open"].get(str(inter.channel.id))
        if info is None:
            await inter.response.send_message("❌ Этот тикет не найден в базе данных.", ephemeral=True)
            return

        if not is_staff(inter.author, *_staff_role_ids(info["type"])):
            await inter.response.send_message(
                "❌ Эта функция доступна только сотрудникам, ведущим этот тикет.", ephemeral=True
            )
            return

        await inter.response.send_modal(AdminReplyModal())

    @disnake.ui.button(label="Закрыть тикет", emoji=icon("lock"), style=disnake.ButtonStyle.secondary, custom_id="ticket_close", row=1)
    async def close_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        data = tickets_store.load()
        info = data["open"].get(str(inter.channel.id))
        if info is None:
            await inter.response.send_message("❌ Этот тикет не найден в базе данных.", ephemeral=True)
            return

        if inter.author.id != info["opener_id"] and not is_staff(inter.author, *_staff_role_ids(info["type"])):
            await inter.response.send_message("❌ Закрыть тикет может только автор или сотрудник.", ephemeral=True)
            return

        await inter.response.send_message(
            "Вы уверены, что хотите закрыть тикет? Канал будет удалён.",
            view=CloseConfirmView(),
            ephemeral=True,
        )


class RejectReasonModal(disnake.ui.Modal):
    def __init__(self, target_channel: disnake.TextChannel, panel_interaction: disnake.MessageInteraction):
        self.target_channel = target_channel
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Причина отказа",
                custom_id="reason",
                style=disnake.TextInputStyle.paragraph,
                max_length=500,
            ),
        ]
        super().__init__(title="Отклонить заявку", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)

        data = tickets_store.load()
        info = data["open"].get(str(self.target_channel.id))
        if info is None:
            await inter.followup.send("❌ Эта заявка не найдена в базе данных.", ephemeral=True)
            return
        if info.get("decision") is not None:
            await inter.followup.send("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        reason = inter.text_values["reason"].strip()
        opener = inter.guild.get_member(info["opener_id"])

        await _remove_recruit_progress_role(inter.guild, opener)

        info["decision"] = "rejected"
        tickets_store.save(data)

        try:
            work_embed = _build_work_embed(self.target_channel, info)
            fresh_view = TicketWorkView(self.target_channel)
            _disable_buttons(fresh_view, ("twork_claim", "twork_accept", "twork_reject"))
            await self.panel_interaction.edit_original_message(embed=work_embed, view=fresh_view)
        except disnake.HTTPException:
            pass

        embed = base_embed(
            f"{icon_tag('cross')} Заявка отклонена",
            f"К сожалению, заявка {opener.mention if opener is not None else 'заявителя'} была отклонена.\n\n"
            "Это не значит, что дверь закрыта навсегда — учтите причину ниже и подавайте заявку "
            "снова, когда будете готовы.",
            color=disnake.Color.red(),
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Решение принял", value=inter.author.mention, inline=False)
        await _post_decision(self.target_channel, info, opener, embed)

        if opener is not None:
            try:
                dm_embed = base_embed(
                    f"{icon_tag('cross')} Ваша заявка отклонена",
                    f"К сожалению, ваша заявка в семью **{FAMILY_NAME}** была отклонена.",
                    color=disnake.Color.red(),
                )
                dm_embed.add_field(name="Причина", value=reason, inline=False)
                dm_embed.add_field(
                    name="Что дальше",
                    value=(
                        "Это не окончательное решение — вы можете подать заявку повторно, "
                        "когда учтёте указанную причину. Мы с радостью рассмотрим её ещё раз."
                    ),
                    inline=False,
                )
                await opener.send(embed=dm_embed)
            except disnake.Forbidden:
                pass

        await _refresh_active_tickets_list(inter.guild)
        await inter.followup.send("✅ Заявка отклонена, автор уведомлён.", ephemeral=True)


class TicketWorkView(disnake.ui.View):
    """Рабочая эфемерная панель для одной конкретной заявки — открывается через выбор
    в списке активных заявок (ActiveTicketsView). Видна только тому, кто её открыл, и
    исчезает после закрытия заявки; список активных заявок при этом остаётся и обновляется."""

    def __init__(self, channel: disnake.TextChannel):
        super().__init__(timeout=600)
        self.channel = channel

        _, info = self._load()
        if info is not None:
            if info.get("claimed_by") is not None:
                _disable_buttons(self, ("twork_claim",))
            if info.get("decision") is not None:
                _disable_buttons(self, ("twork_claim", "twork_accept", "twork_reject"))

    def _load(self) -> tuple[dict, dict | None]:
        data = tickets_store.load()
        return data, data["open"].get(str(self.channel.id))

    @disnake.ui.button(emoji=icon("hand"), style=disnake.ButtonStyle.secondary, custom_id="twork_claim", row=0)
    async def claim_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        data, info = self._load()
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message("❌ Только сотрудники могут забирать заявки.", ephemeral=True)
            return
        claimed_by = info.get("claimed_by")
        if claimed_by is not None:
            await inter.response.send_message(f"❌ Заявку уже забрал <@{claimed_by}>.", ephemeral=True)
            return

        info["claimed_by"] = inter.author.id
        tickets_store.save(data)

        opener = inter.guild.get_member(info["opener_id"])
        role_warning = await _add_recruit_progress_role(inter.guild, opener)

        embed = _build_work_embed(self.channel, info)
        _disable_buttons(self, ("twork_claim",))
        await inter.response.edit_message(embed=embed, view=self)
        if role_warning:
            await inter.followup.send(role_warning, ephemeral=True)

        panel_message_id = info.get("panel_message_id")
        if panel_message_id:
            try:
                panel_message = await self.channel.fetch_message(panel_message_id)
                panel_embed = panel_message.embeds[0]
                for i, field in enumerate(panel_embed.fields):
                    if field.name == "Ответственный":
                        panel_embed.set_field_at(i, name="Ответственный", value=inter.author.mention, inline=True)
                        break
                await panel_message.edit(embed=panel_embed)
            except disnake.HTTPException:
                pass

        call_channel_id = _ticket_config("recruit").get("call_channel_id")
        call_line = f"Ожидаем вас на созвоне: <#{call_channel_id}>" if call_channel_id else "С вами свяжутся в ближайшее время."
        notify_embed = base_embed(
            "📞 Заявка взята в обработку",
            f"Ваша заявка взята в обработку сотрудником {inter.author.mention}.\n{call_line}",
        )
        processing_message = await self.channel.send(
            content=opener.mention if opener is not None else None,
            embed=notify_embed,
        )

        fresh_data = tickets_store.load()
        if str(self.channel.id) in fresh_data["open"]:
            fresh_data["open"][str(self.channel.id)]["processing_message_id"] = processing_message.id
            tickets_store.save(fresh_data)

        await _refresh_active_tickets_list(inter.guild)

    @disnake.ui.button(emoji=icon("shield"), style=disnake.ButtonStyle.secondary, custom_id="twork_admin_reply", row=0)
    async def admin_reply_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        _, info = self._load()
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message(
                "❌ Эта функция доступна только сотрудникам, ведущим эту заявку.", ephemeral=True
            )
            return
        await inter.response.send_modal(AdminReplyModal(target_channel=self.channel))

    @disnake.ui.button(emoji=icon("check"), style=disnake.ButtonStyle.secondary, custom_id="twork_accept", row=0)
    async def accept_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        data, info = self._load()
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message("❌ Только сотрудники могут принимать решения по заявке.", ephemeral=True)
            return
        if info.get("decision") is not None:
            await inter.response.send_message("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        claimed_by = info.get("claimed_by")
        if claimed_by is None:
            await inter.response.send_message(
                f"❌ Сначала заберите заявку кнопкой {icon_tag('hand')} — без этого решение принять нельзя.",
                ephemeral=True,
            )
            return
        if claimed_by != inter.author.id and not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message(
                f"❌ Эту заявку ведёт <@{claimed_by}> — решение может принять только он (или руководство).",
                ephemeral=True,
            )
            return

        subtype = info.get("subtype")
        is_family = subtype == "family"
        opener = inter.guild.get_member(info["opener_id"])
        role_warning = None

        accept_role_id = _ticket_config("recruit").get("accept_role_id")
        if opener is not None and accept_role_id:
            role = inter.guild.get_role(accept_role_id)
            if role is None:
                role_warning = "⚠️ Роль для выдачи не найдена на сервере (проверьте accept_role_id в config.json)."
            else:
                try:
                    await opener.add_roles(role, reason=f"Заявка принята {inter.author} ({inter.author.id})")
                except disnake.Forbidden:
                    role_warning = (
                        f"⚠️ Не удалось выдать роль {role.mention} — роль бота ниже неё в иерархии "
                        "ролей сервера. Поднимите роль бота выше в Настройки → Роли."
                    )

        await _remove_recruit_progress_role(inter.guild, opener)

        info["decision"] = "accepted"
        tickets_store.save(data)

        embed = _build_work_embed(self.channel, info)
        _disable_buttons(self, ("twork_claim", "twork_accept", "twork_reject"))
        await inter.response.edit_message(embed=embed, view=self)
        if role_warning:
            await inter.followup.send(role_warning, ephemeral=True)

        await _post_database_entry(inter.guild, info, opener, inter.author)

        next_step = (
            "🎮 Зайдите в основной стак сервера, чтобы продолжить и приступить к игре в семье.\n"
            "Этот тикет остаётся открытым — если появятся вопросы, пишите прямо сюда."
            if is_family
            else "• Будьте на связи — по орг. вопросам с вами свяжется руководство."
        )
        decision_embed = base_embed(
            f"{icon_tag('check')} Заявка принята",
            f"Поздравляем, {opener.mention if opener is not None else 'участник'}! Заявка одобрена — "
            f"добро пожаловать в семью **{FAMILY_NAME}**.\n\n"
            "**Что дальше:**\n"
            "• Ознакомьтесь с правилами и структурой семьи — с вопросами обращайтесь к куратору.\n"
            f"{next_step}",
            color=disnake.Color.green(),
        )
        decision_embed.add_field(name="Решение принял", value=inter.author.mention, inline=False)
        await _post_decision(self.channel, info, opener, decision_embed)

        if opener is not None:
            try:
                dm_embed = base_embed(
                    f"{icon_tag('check')} Ваша заявка принята!",
                    f"Поздравляем, добро пожаловать в семью **{FAMILY_NAME}**! Мы рады видеть вас в наших рядах.",
                    color=disnake.Color.green(),
                )
                dm_next_step = (
                    "• Зайдите в основной стак сервера, чтобы продолжить и приступить к игре в семье.\n"
                    "• Ваш тикет остаётся открытым — вопросы можно задать прямо там."
                    if is_family
                    else "• Возникнут вопросы — обращайтесь к куратору или руководству."
                )
                dm_embed.add_field(
                    name="Что дальше",
                    value=(
                        "• На сервере уже открыт доступ к внутренним каналам семьи.\n"
                        "• Изучите правила и структуру — это поможет быстрее освоиться.\n"
                        f"{dm_next_step}"
                    ),
                    inline=False,
                )
                await opener.send(embed=dm_embed)
            except disnake.Forbidden:
                pass

        await _refresh_active_tickets_list(inter.guild)

    @disnake.ui.button(emoji=icon("cross"), style=disnake.ButtonStyle.secondary, custom_id="twork_reject", row=0)
    async def reject_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        _, info = self._load()
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message("❌ Только сотрудники могут принимать решения по заявке.", ephemeral=True)
            return
        if info.get("decision") is not None:
            await inter.response.send_message("❌ Решение по этой заявке уже принято.", ephemeral=True)
            return

        claimed_by = info.get("claimed_by")
        if claimed_by is None:
            await inter.response.send_message(
                f"❌ Сначала заберите заявку кнопкой {icon_tag('hand')} — без этого решение принять нельзя.",
                ephemeral=True,
            )
            return
        if claimed_by != inter.author.id and not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message(
                f"❌ Эту заявку ведёт <@{claimed_by}> — решение может принять только он (или руководство).",
                ephemeral=True,
            )
            return

        await inter.response.send_modal(RejectReasonModal(target_channel=self.channel, panel_interaction=inter))

    @disnake.ui.button(emoji=icon("lock"), style=disnake.ButtonStyle.secondary, custom_id="twork_close", row=1)
    async def close_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        _, info = self._load()
        if info is None:
            await inter.response.send_message("❌ Эта заявка уже закрыта.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("recruit")):
            await inter.response.send_message("❌ Закрыть заявку может только сотрудник.", ephemeral=True)
            return
        if info.get("decision") is None:
            await inter.response.send_message(
                "❌ Сначала нужно принять решение по заявке — нажмите «Принять» или «Отклонить».",
                ephemeral=True,
            )
            return

        await inter.response.edit_message(
            content="Вы уверены, что хотите закрыть заявку? Канал будет удалён.",
            embed=None,
            view=TicketWorkCloseConfirmView(self.channel),
        )


class TicketWorkCloseConfirmView(disnake.ui.View):
    """Подтверждение закрытия для заявок — работает в том же эфемерном сообщении, что и
    TicketWorkView, и после закрытия удаляет это сообщение целиком."""

    def __init__(self, channel: disnake.TextChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @disnake.ui.button(label="Подтвердить закрытие", style=disnake.ButtonStyle.danger, custom_id="twork_close_confirm")
    async def confirm(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = self.channel
        await inter.response.edit_message(
            content="🔒 Заявка закрывается, канал будет удалён через несколько секунд...",
            embed=None,
            view=None,
        )

        transcript = await _build_transcript(channel)

        data = tickets_store.load()
        info = data["open"].pop(str(channel.id), None)
        tickets_store.save(data)

        channel_id = log_channel_id("tickets")
        if channel_id:
            log_channel = inter.guild.get_channel(channel_id)
            if log_channel:
                await log_channel.send(embed=_build_close_log_embed(channel, info, inter.author), file=transcript)

        await _refresh_active_tickets_list(inter.guild)

        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Тикет закрыт пользователем {inter.author}")
        except disnake.HTTPException:
            pass

        try:
            await inter.delete_original_message()
        except disnake.HTTPException:
            pass

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="twork_close_cancel")
    async def cancel(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.edit_message(
            content="Закрытие отменено. Выберите заявку заново в списке, чтобы продолжить работу.",
            embed=None,
            view=None,
        )


class CloseConfirmView(disnake.ui.View):
    """Подтверждение закрытия для обычных тикетов (curator/general) прямо в их канале."""

    def __init__(self):
        super().__init__(timeout=60)

    @disnake.ui.button(label="Подтвердить закрытие", style=disnake.ButtonStyle.danger, custom_id="ticket_close_confirm")
    async def confirm(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = inter.channel
        await inter.response.edit_message(content="🔒 Тикет закрывается, канал будет удалён через несколько секунд...", view=None)

        transcript = await _build_transcript(channel)

        data = tickets_store.load()
        info = data["open"].pop(str(channel.id), None)
        tickets_store.save(data)

        channel_id = log_channel_id("tickets")
        if channel_id:
            log_channel = inter.guild.get_channel(channel_id)
            if log_channel:
                await log_channel.send(embed=_build_close_log_embed(channel, info, inter.author), file=transcript)

        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Тикет закрыт пользователем {inter.author}")
        except disnake.HTTPException:
            pass

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="ticket_close_cancel")
    async def cancel(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.edit_message(content="Закрытие тикета отменено.", view=None)


def _ticket_type_label(info: dict) -> str:
    ticket_type = info.get("type")
    if ticket_type == "recruit":
        return _recruit_label(info.get("subtype"))
    labels = {
        "curator": f"{icon_tag('graduation')} Куратор академии",
        "general": f"{icon_tag('ticket')} Служба поддержки",
    }
    return labels.get(ticket_type, ticket_type or "—")


def _build_close_log_embed(
    channel: disnake.TextChannel, info: dict | None, closer: disnake.Member
) -> disnake.Embed:
    embed = base_embed(f"{icon_tag('lock')} Тикет закрыт", color=disnake.Color.dark_grey(), timestamp=True)
    embed.add_field(name="Канал", value=f"#{channel.name}", inline=True)
    embed.add_field(name="Закрыл", value=closer.mention, inline=True)

    if info is not None:
        embed.add_field(name="Тип", value=_ticket_type_label(info), inline=True)
        embed.add_field(name="Автор", value=f"<@{info['opener_id']}>", inline=True)
        claimed_by = info.get("claimed_by")
        embed.add_field(
            name="Ответственный",
            value=f"<@{claimed_by}>" if claimed_by else f"{icon_tag('unassigned')} не назначен",
            inline=True,
        )
        decision = info.get("decision")
        if decision:
            embed.add_field(
                name="Решение",
                value=f"{icon_tag('check')} Принята" if decision == "accepted" else f"{icon_tag('cross')} Отклонена",
                inline=True,
            )

        created_at = info.get("created_at")
        if created_at:
            try:
                started = dt.datetime.fromisoformat(created_at)
                total_minutes = max(0, int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() // 60))
                hours, minutes = divmod(total_minutes, 60)
                duration_text = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
                embed.add_field(name="Длительность", value=duration_text, inline=True)
            except ValueError:
                pass

    embed.set_footer(text=f"Тикет #{info.get('number')} • Семья RESTRUCT" if info else "Семья RESTRUCT")
    return embed


async def _build_transcript(channel: disnake.TextChannel) -> disnake.File:
    lines = []
    async for message in channel.history(limit=500, oldest_first=True):
        timestamp = message.created_at.strftime("%d.%m.%Y %H:%M")
        content = message.content or "[вложение/embed без текста]"
        lines.append(f"[{timestamp}] {message.author}: {content}")
    buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
    return disnake.File(buffer, filename=f"{channel.name}-transcript.txt")


class Tickets(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    async def _send_panel(self, inter: disnake.ApplicationCommandInteraction, key: str, view: disnake.ui.View):
        panel = PANELS[key]
        title = f"{icon_tag(panel['icon'])} {panel['title']}"
        embed = base_embed(title, panel["description"], panel_key=key)
        await send_panel(inter, embed, view=view, panel_key=key)

    @commands.slash_command(name="curator", description="Создать панель тикетов для куратора академии", default_member_permissions=ADMIN_PERMS)
    async def curator(self, inter: disnake.ApplicationCommandInteraction):
        await self._send_panel(inter, "curator", _make_open_button_view("curator"))

    @commands.slash_command(name="recruit", description="Создать панель тикетов для рекрута", default_member_permissions=ADMIN_PERMS)
    async def recruit(self, inter: disnake.ApplicationCommandInteraction):
        await self._send_panel(inter, "recruit", RecruitTicketView())

    @commands.slash_command(name="ticket", description="Создать основную панель для создания тикетов", default_member_permissions=ADMIN_PERMS)
    async def ticket(self, inter: disnake.ApplicationCommandInteraction):
        await self._send_panel(inter, "general", GeneralTicketView())

    @commands.slash_command(
        name="moderation",
        description="Опубликовать панель активных заявок в этом канале",
        default_member_permissions=ADMIN_PERMS,
    )
    async def moderation(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.send_message("✅ Панель опубликована в этом канале.", ephemeral=True)

        data = tickets_store.load()
        data["active_list_channel_id"] = inter.channel.id
        data["active_list_message_id"] = None
        tickets_store.save(data)

        await _refresh_active_tickets_list(inter.guild)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Tickets(bot))


PERSISTENT_VIEWS = [
    lambda: TicketControlView(),
    lambda: _build_active_tickets_view(),
    lambda: GeneralTicketView(),
    lambda: RecruitTicketView(),
] + [
    (lambda key=key: _make_open_button_view(key)) for key in ("curator",)
]
