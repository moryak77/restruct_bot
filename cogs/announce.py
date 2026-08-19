from __future__ import annotations

import datetime as dt

import disnake
from disnake.ext import commands

from cogs.moderation import _is_leader
from core.branding import base_embed, send_panel
from core.config import config
from core.icons import icon, icon_tag
from core.storage import announce_store, binds_store

MAX_LINK_BUTTONS = 15  # 3 ряда по 5 — четвёртый ряд под текст/теги/кнопки/цвет/реакции, пятый под действия
MAX_TAG_ROLES = 5

# Базовые цвета на выбор вместо ввода HEX-кода — (ключ, эмодзи-свотч, подпись, disnake.Color)
COLOR_PALETTE: list[tuple[str, str, str, disnake.Color]] = [
    ("red", "🔴", "Красный (по умолчанию)", disnake.Color.red()),
    ("orange", "🟠", "Оранжевый", disnake.Color.orange()),
    ("yellow", "🟡", "Жёлтый", disnake.Color.gold()),
    ("green", "🟢", "Зелёный", disnake.Color.green()),
    ("blue", "🔵", "Синий", disnake.Color.blue()),
    ("purple", "🟣", "Фиолетовый", disnake.Color.purple()),
    ("black", "⚫", "Чёрный", disnake.Color.from_rgb(35, 35, 38)),
]
COLOR_BY_KEY: dict[str, disnake.Color] = {key: value for key, _, _, value in COLOR_PALETTE}


def _draft_color(key: str) -> disnake.Color:
    return COLOR_BY_KEY.get(key, COLOR_BY_KEY["red"])


def _channel_ids() -> list[int]:
    return [c for c in (config.get("restruct_announce.channel_ids", []) or []) if c]


def _default_role_ids() -> list[int]:
    role_id = config.get("restruct_announce.role_id")
    return [role_id] if role_id else []


# ---------------------------------------------------------------------------
# Черновик объявления/бинда — живёт в памяти, пока открыт редактор. Один и тот же
# редактор обслуживает и разовую рассылку (is_bind=False), и создание/правку личного
# бинда (is_bind=True) — отличаются только кнопки действий внизу.
# ---------------------------------------------------------------------------

class AnnounceDraft:
    def __init__(self, *, is_bind: bool = False, bind_id: int | None = None, name: str = ""):
        self.is_bind = is_bind
        self.bind_id = bind_id  # None, пока бинд ни разу не сохранён
        self.name = name
        self.title = ""
        self.body = ""
        self.color = "red"  # ключ из COLOR_PALETTE
        self.role_ids: list[int] = []
        self.buttons: list[dict] = []  # [{"label": str, "url": str, "emoji": str | None}]
        self.reactions: list[str] = []  # эмодзи-строки

    @classmethod
    def from_bind(cls, bind: dict) -> "AnnounceDraft":
        draft = cls(is_bind=True, bind_id=bind["id"], name=bind["name"])
        draft.title = bind.get("title", "")
        draft.body = bind.get("body", "")
        draft.color = bind.get("color") or "red"  # старые бинды без цвета — красный, как раньше
        draft.role_ids = list(bind.get("role_ids") or [])
        draft.buttons = [dict(b) for b in (bind.get("buttons") or [])]
        draft.reactions = list(bind.get("reactions") or [])
        return draft


def _my_binds(owner_id: int) -> list[dict]:
    data = binds_store.load()
    return sorted(
        (b for b in data["binds"].values() if b["owner_id"] == owner_id),
        key=lambda b: b["name"].lower(),
    )


def _save_bind(owner_id: int, draft: AnnounceDraft) -> None:
    data = binds_store.load()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if draft.bind_id is None:
        bind_id = data["next_id"]
        data["next_id"] += 1
        draft.bind_id = bind_id
        created_at = now
    else:
        bind_id = draft.bind_id
        created_at = data["binds"].get(str(bind_id), {}).get("created_at", now)
    data["binds"][str(bind_id)] = {
        "id": bind_id,
        "owner_id": owner_id,
        "name": draft.name,
        "title": draft.title,
        "body": draft.body,
        "color": draft.color,
        "role_ids": draft.role_ids,
        "buttons": draft.buttons,
        "reactions": draft.reactions,
        "created_at": created_at,
        "updated_at": now,
    }
    binds_store.save(data)


def _delete_bind(bind_id: int) -> None:
    data = binds_store.load()
    data["binds"].pop(str(bind_id), None)
    binds_store.save(data)


# ---------------------------------------------------------------------------
# Embed'ы
# ---------------------------------------------------------------------------

def _build_content_embed(draft: AnnounceDraft) -> disnake.Embed:
    """Чистый embed «для зрителей» — именно он уходит в каналы и в ЛС, без служебных полей."""
    return base_embed(draft.title or "Без заголовка", draft.body or "—", color=_draft_color(draft.color), timestamp=True)


def _build_preview_embed(draft: AnnounceDraft) -> disnake.Embed:
    """Тот же embed, но с добавленными служебными полями — что уйдёт в теги и какие
    реакции проставятся. Виден только автору в редакторе, никогда не публикуется."""
    embed = _build_content_embed(draft)
    tag_value = " ".join(f"<@&{rid}>" for rid in draft.role_ids) if draft.role_ids else "не выбраны"
    embed.add_field(name=f"{icon_tag('tag')} Теги при отправке", value=tag_value, inline=True)
    reactions_value = " ".join(draft.reactions) if draft.reactions else "нет"
    embed.add_field(name=f"{icon_tag('smile')} Реакции при отправке", value=reactions_value, inline=True)
    embed.add_field(name=f"{icon_tag('link')} Кнопки-ссылки", value=str(len(draft.buttons)) or "0", inline=True)
    return embed


def _build_binds_home_embed(owner_id: int, binds: list[dict]) -> disnake.Embed:
    if binds:
        return base_embed(
            f"{icon_tag('bind')} Ваши бинды",
            f"Сохранено: **{len(binds)}**. Выберите готовый в списке ниже, чтобы отправить в один клик, "
            "или создайте новый.",
            color=disnake.Color.blurple(),
        )
    return base_embed(
        f"{icon_tag('bind')} У вас пока нет биндов",
        "Создайте первый — заголовок, текст, теги и кнопки сохранятся, и дальше рассылка будет в один клик.",
        color=disnake.Color.blurple(),
    )


def _build_bind_summary_embed(bind: dict) -> disnake.Embed:
    embed = base_embed(f"{icon_tag('bind')} {bind['name']}", color=disnake.Color.blurple())
    embed.add_field(name="Заголовок", value=bind.get("title") or "—", inline=False)
    embed.add_field(name="Текст", value=(bind.get("body") or "—")[:500], inline=False)
    role_ids = bind.get("role_ids") or []
    embed.add_field(name=f"{icon_tag('tag')} Теги", value=" ".join(f"<@&{r}>" for r in role_ids) or "нет", inline=True)
    embed.add_field(name=f"{icon_tag('link')} Кнопки", value=str(len(bind.get("buttons") or [])), inline=True)
    embed.add_field(name=f"{icon_tag('smile')} Реакции", value=" ".join(bind.get("reactions") or []) or "нет", inline=True)
    return embed


def _make_link_button(data: dict, row: int) -> disnake.ui.Button:
    return disnake.ui.Button(
        label=(data.get("label") or "Ссылка")[:80],
        url=data["url"],
        style=disnake.ButtonStyle.link,
        emoji=data.get("emoji") or None,
        row=row,
    )


async def _render_composer(edit_fn, draft: AnnounceDraft) -> None:
    await edit_fn(content=None, embed=_build_preview_embed(draft), view=ComposerView(draft))


def _status_embed(icon_key: str, title: str, description: str | None = None, *, color: disnake.Color | None = None) -> disnake.Embed:
    """Единый вид для всех коротких служебных сообщений редактора (сохранено/удалено/отменено
    и т.д.) — с иконкой в заголовке вместо голого текста."""
    return base_embed(f"{icon_tag(icon_key)} {title}", description, color=color)


# ---------------------------------------------------------------------------
# Публикация — общая логика для разовых объявлений и «Отправить сейчас» у бинда.
# ---------------------------------------------------------------------------

async def _publish_draft(guild: disnake.Guild, draft: AnnounceDraft) -> disnake.Embed:
    channel_ids = _channel_ids()
    if not channel_ids:
        return _status_embed(
            "alert", "Каналы не настроены",
            "Проверьте `restruct_announce.channel_ids` в config.json.",
            color=disnake.Color.red(),
        )

    embed = _build_content_embed(draft)
    roles = [role for rid in draft.role_ids if (role := guild.get_role(rid)) is not None]
    content = " ".join(role.mention for role in roles) or None

    link_buttons = [_make_link_button(d, i // 5) for i, d in enumerate(draft.buttons[:MAX_LINK_BUTTONS])]
    view = None
    if link_buttons:
        view = disnake.ui.View(timeout=None)
        for b in link_buttons:
            view.add_item(b)

    sent_messages: dict[str, int] = {}
    failed_channels: list[int] = []
    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id)
        if channel is None:
            failed_channels.append(channel_id)
            continue
        try:
            message = await channel.send(
                content=content, embed=embed, view=view, allowed_mentions=disnake.AllowedMentions(roles=True)
            )
        except disnake.HTTPException:
            failed_channels.append(channel_id)
            continue
        sent_messages[str(channel_id)] = message.id
        for emoji in draft.reactions:
            try:
                await message.add_reaction(emoji)
            except disnake.HTTPException:
                pass  # битый/недоступный боту эмодзи — пропускаем, остальные реакции всё равно проставятся

    if not draft.is_bind:
        # Разовые объявления запоминаем в announce_store — так /restruct_announce edit
        # по-прежнему может поправить именно последнюю разовую рассылку.
        announce_store.save({"title": draft.title, "body": draft.body, "messages": sent_messages})

    dm_ok = dm_failed = 0
    notified_ids: set[int] = set()
    for role in roles:
        for member in role.members:
            if member.bot or member.id in notified_ids:
                continue
            notified_ids.add(member.id)
            try:
                await member.send(embed=embed, view=view)
                dm_ok += 1
            except (disnake.Forbidden, disnake.HTTPException):
                dm_failed += 1

    ok = not failed_channels and (not roles or dm_failed == 0)
    result = _status_embed(
        "check" if ok else "alert",
        "Объявление опубликовано" if ok else "Опубликовано с замечаниями",
        color=disnake.Color.green() if ok else disnake.Color.orange(),
    )
    result.add_field(
        name=f"{icon_tag('link')} Каналы", value=f"{len(sent_messages)} из {len(channel_ids)}", inline=True
    )
    if failed_channels:
        result.add_field(
            name=f"{icon_tag('cross')} Не удалось отправить в",
            value=", ".join(f"<#{c}>" for c in failed_channels),
            inline=False,
        )
    if roles:
        role_list = ", ".join(role.mention for role in roles)
        result.add_field(
            name=f"{icon_tag('users')} Тег и рассылка в ЛС",
            value=f"Роли: {role_list}\nДоставлено: **{dm_ok}**, не доставлено: **{dm_failed}**",
            inline=False,
        )
    else:
        result.add_field(
            name=f"{icon_tag('tag')} Теги",
            value="Роль для тега не выбрана — ЛС не рассылались (кнопка «Теги»).",
            inline=False,
        )
    return result


# ---------------------------------------------------------------------------
# Главный экран редактора (разовое объявление или бинд)
# ---------------------------------------------------------------------------

class TextControlButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Текст", emoji=icon("pencil"), style=disnake.ButtonStyle.secondary, row=3)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(AnnounceTextModal(self.draft, inter))


class ColorControlButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Цвет", emoji=icon("palette"), style=disnake.ButtonStyle.secondary, row=3)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(view=ColorView(self.draft))


class TagsControlButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Теги", emoji=icon("tag"), style=disnake.ButtonStyle.secondary, row=3)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(view=TagsView(self.draft))


class ButtonsControlButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Кнопки", emoji=icon("link"), style=disnake.ButtonStyle.secondary, row=3)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(view=ButtonsView(self.draft))


class ReactionsControlButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Реакции", emoji=icon("smile"), style=disnake.ButtonStyle.secondary, row=3)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(ReactionsModal(self.draft, inter))


class PublishButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Опубликовать", emoji=icon("check"), style=disnake.ButtonStyle.primary, row=4)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        if not self.draft.title.strip() and not self.draft.body.strip():
            await inter.response.send_message(
                "❌ Сначала заполните текст объявления — кнопка «Текст».", ephemeral=True
            )
            return
        await inter.response.defer()
        result_embed = await _publish_draft(inter.guild, self.draft)
        await inter.edit_original_message(content=None, embed=result_embed, view=None)


class CancelButton(disnake.ui.Button):
    def __init__(self, label: str = "Отмена"):
        super().__init__(label=label, emoji=icon("cross"), style=disnake.ButtonStyle.secondary, row=4)

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(
            content=None, embed=_status_embed("cross", "Действие отменено"), view=None
        )


class SaveBindButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Сохранить", emoji=icon("save"), style=disnake.ButtonStyle.primary, row=4)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        _save_bind(inter.author.id, self.draft)
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))
        await inter.followup.send(
            embed=_status_embed("save", "Бинд сохранён", f"«{self.draft.name}»", color=disnake.Color.green()),
            ephemeral=True,
        )


class SendBindNowButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Отправить сейчас", emoji=icon("send"), style=disnake.ButtonStyle.success, row=4)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        if not self.draft.title.strip() and not self.draft.body.strip():
            await inter.response.send_message(
                "❌ Сначала заполните текст — кнопка «Текст».", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        result_embed = await _publish_draft(inter.guild, self.draft)
        await inter.followup.send(embed=result_embed, ephemeral=True)


class DeleteBindButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Удалить", emoji=icon("delete"), style=disnake.ButtonStyle.danger, row=4)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        if self.draft.bind_id is not None:
            _delete_bind(self.draft.bind_id)
        await inter.response.edit_message(
            content=None,
            embed=_status_embed("delete", "Бинд удалён", f"«{self.draft.name}»"),
            view=None,
        )


class ComposerView(disnake.ui.View):
    """Эфемерная панель редактирования — видна только автору. Ряды 0–2 — кнопки-ссылки
    самого объявления, ряд 3 — управление (текст/цвет/теги/кнопки/реакции), ряд 4 — действия
    (публикация для разовых объявлений; сохранить/отправить/удалить для бинда)."""

    def __init__(self, draft: AnnounceDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        for i, data in enumerate(draft.buttons[:MAX_LINK_BUTTONS]):
            self.add_item(_make_link_button(data, row=i // 5))

        self.add_item(TextControlButton(draft))
        self.add_item(ColorControlButton(draft))
        self.add_item(TagsControlButton(draft))
        self.add_item(ButtonsControlButton(draft))
        self.add_item(ReactionsControlButton(draft))

        if draft.is_bind:
            self.add_item(SaveBindButton(draft))
            self.add_item(SendBindNowButton(draft))
            if draft.bind_id is not None:
                self.add_item(DeleteBindButton(draft))
            self.add_item(CancelButton("Закрыть"))
        else:
            self.add_item(PublishButton(draft))
            self.add_item(CancelButton("Отмена"))


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

class AnnounceTextModal(disnake.ui.Modal):
    def __init__(self, draft: AnnounceDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Заголовок",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=256,
                required=False,
                value=draft.title or None,
                placeholder="Например: Важное объявление",
            ),
            disnake.ui.TextInput(
                label="Текст сообщения",
                custom_id="body",
                style=disnake.TextInputStyle.paragraph,
                max_length=3900,
                required=False,
                value=draft.body or None,
            ),
        ]
        super().__init__(title="Текст объявления", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        self.draft.title = inter.text_values["title"].strip()
        self.draft.body = inter.text_values["body"].strip()
        await inter.response.defer(ephemeral=True)
        await _render_composer(self.panel_interaction.edit_original_message, self.draft)


# ---------------------------------------------------------------------------
# Цвет — только готовые базовые цвета, без ввода HEX-кода вручную
# ---------------------------------------------------------------------------

class ColorSelect(disnake.ui.StringSelect):
    def __init__(self, draft: AnnounceDraft):
        self.draft = draft
        options = [
            disnake.SelectOption(label=label, value=key, emoji=swatch, default=(key == draft.color))
            for key, swatch, label, _ in COLOR_PALETTE
        ]
        super().__init__(placeholder="Выберите цвет полосы embed'а", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        self.draft.color = self.values[0]
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))


class ColorBackButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Назад", emoji=icon("back"), style=disnake.ButtonStyle.secondary, row=1)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))


class ColorView(disnake.ui.View):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        self.add_item(ColorSelect(draft))
        self.add_item(ColorBackButton(draft))


# ---------------------------------------------------------------------------
# Теги (роли)
# ---------------------------------------------------------------------------

class TagsRoleSelect(disnake.ui.RoleSelect):
    def __init__(self, draft: AnnounceDraft):
        self.draft = draft
        super().__init__(
            placeholder="Роли для тега и рассылки в ЛС (можно несколько)",
            min_values=0,
            max_values=MAX_TAG_ROLES,
        )

    async def callback(self, inter: disnake.MessageInteraction):
        self.draft.role_ids = [role.id for role in self.values]
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))


class TagsBackButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Назад", emoji=icon("back"), style=disnake.ButtonStyle.secondary, row=1)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))


class TagsView(disnake.ui.View):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        self.add_item(TagsRoleSelect(draft))
        self.add_item(TagsBackButton(draft))


# ---------------------------------------------------------------------------
# Кнопки-ссылки
# ---------------------------------------------------------------------------

class AddButtonModal(disnake.ui.Modal):
    def __init__(self, draft: AnnounceDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(label="Текст на кнопке", custom_id="label", style=disnake.TextInputStyle.short, max_length=80),
            disnake.ui.TextInput(
                label="Ссылка (начинается с http:// или https://)",
                custom_id="url",
                style=disnake.TextInputStyle.short,
                max_length=512,
            ),
            disnake.ui.TextInput(
                label="Эмодзи (необязательно)", custom_id="emoji", style=disnake.TextInputStyle.short, max_length=10, required=False
            ),
        ]
        super().__init__(title="Добавить кнопку-ссылку", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        url = inter.text_values["url"].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await inter.response.send_message("❌ Ссылка должна начинаться с http:// или https://", ephemeral=True)
            return

        entry = {
            "label": inter.text_values["label"].strip() or "Ссылка",
            "url": url,
            "emoji": inter.text_values["emoji"].strip() or None,
        }
        self.draft.buttons.append(entry)

        await inter.response.defer(ephemeral=True)
        try:
            await self.panel_interaction.edit_original_message(view=ButtonsView(self.draft))
        except disnake.HTTPException as exc:
            self.draft.buttons.remove(entry)
            await inter.followup.send(f"❌ Discord отклонил кнопку (возможно, некорректное эмодзи): {exc}", ephemeral=True)
            return
        await inter.followup.send(embed=_status_embed("check", "Кнопка добавлена", color=disnake.Color.green()), ephemeral=True)


class RemoveButtonSelect(disnake.ui.StringSelect):
    def __init__(self, draft: AnnounceDraft):
        self.draft = draft
        options = [
            disnake.SelectOption(label=b["label"][:100] or "Без названия", value=str(i), description=b["url"][:100])
            for i, b in enumerate(draft.buttons)
        ]
        super().__init__(placeholder="Удалить кнопку…", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        idx = int(self.values[0])
        if 0 <= idx < len(self.draft.buttons):
            self.draft.buttons.pop(idx)
        await inter.response.edit_message(view=ButtonsView(self.draft))


class AddButtonButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Добавить кнопку", emoji=icon("plus"), style=disnake.ButtonStyle.secondary, row=1)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        if len(self.draft.buttons) >= MAX_LINK_BUTTONS:
            await inter.response.send_message(f"❌ Уже добавлено максимум кнопок ({MAX_LINK_BUTTONS}).", ephemeral=True)
            return
        await inter.response.send_modal(AddButtonModal(self.draft, inter))


class ButtonsBackButton(disnake.ui.Button):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(label="Назад", emoji=icon("back"), style=disnake.ButtonStyle.secondary, row=1)
        self.draft = draft

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.edit_message(embed=_build_preview_embed(self.draft), view=ComposerView(self.draft))


class ButtonsView(disnake.ui.View):
    def __init__(self, draft: AnnounceDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        if draft.buttons:
            self.add_item(RemoveButtonSelect(draft))
        self.add_item(AddButtonButton(draft))
        self.add_item(ButtonsBackButton(draft))


# ---------------------------------------------------------------------------
# Реакции
# ---------------------------------------------------------------------------

class ReactionsModal(disnake.ui.Modal):
    def __init__(self, draft: AnnounceDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Эмодзи через пробел — реакции на сообщение",
                custom_id="reactions",
                style=disnake.TextInputStyle.short,
                max_length=200,
                required=False,
                value=" ".join(draft.reactions) or None,
                placeholder="Например: ✅ 🔥 👍",
            ),
        ]
        super().__init__(title="Реакции на сообщение", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["reactions"].strip()
        emojis = raw.split() if raw else []
        if len(emojis) > 20:
            await inter.response.send_message("❌ Слишком много реакций — не больше 20.", ephemeral=True)
            return
        self.draft.reactions = emojis
        await inter.response.defer(ephemeral=True)
        await _render_composer(self.panel_interaction.edit_original_message, self.draft)


# ---------------------------------------------------------------------------
# Бинды — персональные шаблоны рассылки для каждого owner/dep.own
# ---------------------------------------------------------------------------

class BindNameModal(disnake.ui.Modal):
    def __init__(self, panel_interaction: disnake.MessageInteraction):
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Название бинда",
                custom_id="name",
                style=disnake.TextInputStyle.short,
                max_length=60,
                placeholder="Например: Набор в семью",
            ),
        ]
        super().__init__(title="Новый бинд", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        name = inter.text_values["name"].strip()
        if not name:
            await inter.response.send_message("❌ Название не может быть пустым.", ephemeral=True)
            return
        draft = AnnounceDraft(is_bind=True, name=name)
        draft.role_ids = _default_role_ids()
        await inter.response.defer(ephemeral=True)
        await _render_composer(self.panel_interaction.edit_original_message, draft)


class BindPickSelect(disnake.ui.StringSelect):
    def __init__(self, binds: list[dict]):
        self.binds_by_id = {str(b["id"]): b for b in binds}
        options = [
            disnake.SelectOption(label=b["name"][:100], value=str(b["id"]), description=(b.get("title") or "без заголовка")[:100])
            for b in binds
        ]
        super().__init__(placeholder="Выберите бинд", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        bind = self.binds_by_id[self.values[0]]
        if bind["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Это не ваш бинд.", ephemeral=True)
            return
        await inter.response.edit_message(embed=_build_bind_summary_embed(bind), view=BindActionsView(bind))


class CreateBindButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(label="Создать бинд", emoji=icon("plus"), style=disnake.ButtonStyle.success, row=1)

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(BindNameModal(inter))


class BindsHomeView(disnake.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        binds = _my_binds(owner_id)
        if binds:
            self.add_item(BindPickSelect(binds))
        self.add_item(CreateBindButton())


class BindSendButton(disnake.ui.Button):
    def __init__(self, bind: dict):
        super().__init__(label="Отправить", emoji=icon("send"), style=disnake.ButtonStyle.success, row=0)
        self.bind = bind

    async def callback(self, inter: disnake.MessageInteraction):
        if self.bind["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Это не ваш бинд.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        draft = AnnounceDraft.from_bind(self.bind)
        result_embed = await _publish_draft(inter.guild, draft)
        await inter.followup.send(embed=result_embed, ephemeral=True)


class BindEditButton(disnake.ui.Button):
    def __init__(self, bind: dict):
        super().__init__(label="Редактировать", emoji=icon("pencil"), style=disnake.ButtonStyle.primary, row=0)
        self.bind = bind

    async def callback(self, inter: disnake.MessageInteraction):
        if self.bind["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Это не ваш бинд.", ephemeral=True)
            return
        draft = AnnounceDraft.from_bind(self.bind)
        await inter.response.defer(ephemeral=True)
        await _render_composer(inter.edit_original_message, draft)


class BindDeleteButton(disnake.ui.Button):
    def __init__(self, bind: dict):
        super().__init__(label="Удалить", emoji=icon("delete"), style=disnake.ButtonStyle.danger, row=0)
        self.bind = bind

    async def callback(self, inter: disnake.MessageInteraction):
        if self.bind["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Это не ваш бинд.", ephemeral=True)
            return
        await inter.response.edit_message(
            content=None,
            embed=_status_embed(
                "alert", "Удалить бинд?", f"«{self.bind['name']}» — это нельзя отменить.", color=disnake.Color.orange()
            ),
            view=ConfirmDeleteBindView(self.bind),
        )


class BindBackButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(label="Назад", emoji=icon("back"), style=disnake.ButtonStyle.secondary, row=0)

    async def callback(self, inter: disnake.MessageInteraction):
        binds = _my_binds(inter.author.id)
        await inter.response.edit_message(
            content=None, embed=_build_binds_home_embed(inter.author.id, binds), view=BindsHomeView(inter.author.id)
        )


class BindActionsView(disnake.ui.View):
    def __init__(self, bind: dict):
        super().__init__(timeout=300)
        self.bind = bind
        self.add_item(BindSendButton(bind))
        self.add_item(BindEditButton(bind))
        self.add_item(BindDeleteButton(bind))
        self.add_item(BindBackButton())


class ConfirmDeleteBindView(disnake.ui.View):
    def __init__(self, bind: dict):
        super().__init__(timeout=120)
        self.bind = bind

    @disnake.ui.button(label="Да, удалить", emoji=icon("delete"), style=disnake.ButtonStyle.danger)
    async def confirm(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if self.bind["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Это не ваш бинд.", ephemeral=True)
            return
        _delete_bind(self.bind["id"])
        await inter.response.edit_message(
            content=None, embed=_status_embed("delete", "Бинд удалён", f"«{self.bind['name']}»"), view=None
        )

    @disnake.ui.button(label="Отмена", emoji=icon("cross"), style=disnake.ButtonStyle.secondary)
    async def cancel(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.edit_message(
            content=None, embed=_build_bind_summary_embed(self.bind), view=BindActionsView(self.bind)
        )


# ---------------------------------------------------------------------------
# Персистентная панель
# ---------------------------------------------------------------------------

def _build_panel_embed() -> disnake.Embed:
    return base_embed(
        f"{icon_tag('announce')} Центр объявлений RESTRUCT",
        (
            "Отсюда руководство публикует объявления сразу в закреплённые каналы — с тегом роли "
            "и рассылкой в личные сообщения.\n\n"
            f"**{icon_tag('announce')} Создать объявление** — разовая рассылка: текст, теги, "
            "кнопки-ссылки, реакции.\n"
            f"**{icon_tag('bind')} Мои бинды** — личные сохранённые шаблоны: настроили один раз — "
            "дальше отправка в один клик. У каждого в руководстве свой набор биндов."
        ),
        panel_key="announce",
    )


class AnnounceCreateButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(
            label="Создать объявление", emoji=icon("announce"), style=disnake.ButtonStyle.primary,
            custom_id="announce_panel_create",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        draft = AnnounceDraft()
        draft.role_ids = _default_role_ids()
        await inter.response.send_message(
            "👀 **Редактор объявления — видно только вам.** Заполните текст кнопкой «Текст», при "
            "необходимости настройте теги/кнопки/реакции, затем нажмите «Опубликовать».",
            embed=_build_preview_embed(draft),
            view=ComposerView(draft),
            ephemeral=True,
        )


class AnnounceBindsButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(
            label="Мои бинды", emoji=icon("bind"), style=disnake.ButtonStyle.secondary,
            custom_id="announce_panel_binds",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        binds = _my_binds(inter.author.id)
        await inter.response.send_message(
            embed=_build_binds_home_embed(inter.author.id, binds), view=BindsHomeView(inter.author.id), ephemeral=True
        )


class AnnouncePanelView(disnake.ui.View):
    """Постоянная панель — публикуется один раз командой /restruct_announce panel и висит
    в канале; персистентные custom_id у кнопок, регистрируется в PERSISTENT_VIEWS."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AnnounceCreateButton())
        self.add_item(AnnounceBindsButton())


# ---------------------------------------------------------------------------
# Слэш-команды (старый способ «по команде» остаётся доступен как есть)
# ---------------------------------------------------------------------------

class AnnounceSendModal(disnake.ui.Modal):
    """Публикует одно и то же объявление сразу в оба закреплённых канала (restruct_announce.
    channel_ids), тегает роль restruct_announce.role_id в этих сообщениях и параллельно
    рассылает его же в ЛС каждому участнику с этой ролью — так рассылка не теряется даже
    для тех, кто редко заглядывает в сами каналы."""

    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Заголовок",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=256,
                placeholder="Например: Важное объявление",
            ),
            disnake.ui.TextInput(
                label="Текст сообщения",
                custom_id="body",
                style=disnake.TextInputStyle.paragraph,
                max_length=3900,
            ),
        ]
        super().__init__(title="Рассылка RESTRUCT", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        draft = AnnounceDraft()
        draft.title = inter.text_values["title"].strip()
        draft.body = inter.text_values["body"].strip()
        draft.role_ids = _default_role_ids()
        result_embed = await _publish_draft(inter.guild, draft)
        await inter.followup.send(embed=result_embed, ephemeral=True)


class AnnounceEditModal(disnake.ui.Modal):
    """Редактирует уже отправленное разовое объявление сразу в обоих каналах — по
    сохранённым message_id, без повторной рассылки в ЛС (иначе каждая правка спамила бы
    всех заново)."""

    def __init__(self, current: dict):
        components = [
            disnake.ui.TextInput(
                label="Заголовок",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=256,
                value=current.get("title", ""),
            ),
            disnake.ui.TextInput(
                label="Текст сообщения",
                custom_id="body",
                style=disnake.TextInputStyle.paragraph,
                max_length=3900,
                value=current.get("body", ""),
            ),
        ]
        super().__init__(title="Редактировать рассылку", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)

        title = inter.text_values["title"].strip()
        body = inter.text_values["body"].strip()
        edit_draft = AnnounceDraft()
        edit_draft.title, edit_draft.body = title, body
        embed = _build_content_embed(edit_draft)

        data = announce_store.load()
        updated, failed = 0, []
        for channel_id_str, message_id in data.get("messages", {}).items():
            channel = inter.guild.get_channel(int(channel_id_str))
            if channel is None:
                failed.append(channel_id_str)
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)
                updated += 1
            except disnake.HTTPException:
                failed.append(channel_id_str)

        data["title"] = title
        data["body"] = body
        announce_store.save(data)

        result_embed = _status_embed(
            "check" if not failed else "alert",
            "Рассылка обновлена",
            color=disnake.Color.green() if not failed else disnake.Color.orange(),
        )
        result_embed.add_field(name=f"{icon_tag('link')} Обновлено сообщений", value=str(updated), inline=True)
        if failed:
            result_embed.add_field(
                name=f"{icon_tag('cross')} Не удалось обновить",
                value=", ".join(f"<#{c}>" for c in failed),
                inline=False,
            )
        await inter.followup.send(embed=result_embed, ephemeral=True)


class Announce(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="restruct_announce", description="Рассылка объявления (только для руководства)")
    async def restruct_announce(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @restruct_announce.sub_command(
        name="send", description="Отправить объявление в закреплённые каналы и в ЛС роли restruct"
    )
    async def announce_send(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await inter.response.send_modal(AnnounceSendModal())

    @restruct_announce.sub_command(name="edit", description="Отредактировать уже отправленное объявление")
    async def announce_edit(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        data = announce_store.load()
        if not data.get("messages"):
            await inter.response.send_message(
                "❌ Ещё не было отправлено ни одного объявления — сначала `/restruct_announce send`.", ephemeral=True
            )
            return
        await inter.response.send_modal(AnnounceEditModal(data))

    @restruct_announce.sub_command(
        name="panel", description="Опубликовать панель объявлений и биндов (только для руководства)"
    )
    async def announce_panel(self, inter: disnake.ApplicationCommandInteraction):
        if not _is_leader(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await send_panel(inter, _build_panel_embed(), view=AnnouncePanelView(), panel_key="announce")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Announce(bot))


PERSISTENT_VIEWS = [lambda: AnnouncePanelView()]
