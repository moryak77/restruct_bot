from __future__ import annotations

import io

import disnake
from disnake.ext import commands

from core.branding import BRAND_COLOR, panel_file_kwargs
from core.config import config

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

MAX_LINK_BUTTONS = 15  # 3 ряда по 5 — четвёртый ряд всегда зарезервирован под управление


class NewsDraft:
    """Состояние черновика новости, которое живёт в памяти, пока идёт редактирование —
    от первого открытия команды и до публикации."""

    def __init__(self, channel: disnake.TextChannel, image: disnake.Attachment | None = None):
        self.channel = channel
        self.image = image
        self.title = ""
        self.body = ""
        self.color: int | None = None
        self.buttons: list[dict] = []  # [{"label": str, "url": str, "emoji": str | None}]


def _validate_image(image: disnake.Attachment) -> bool:
    is_image_type = (image.content_type or "").startswith("image/")
    is_image_ext = image.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
    return is_image_type or is_image_ext


async def _resolve_image_file(image: disnake.Attachment | None) -> disnake.File | None:
    if image is not None:
        data = await image.read()
        return disnake.File(io.BytesIO(data), filename=image.filename)
    fallback = panel_file_kwargs("news")
    return fallback.get("file")


def _draft_color(draft: NewsDraft) -> disnake.Color:
    return disnake.Color(draft.color) if draft.color is not None else BRAND_COLOR


def _build_embed(draft: NewsDraft, author: disnake.Member, image_file: disnake.File | None) -> disnake.Embed:
    embed = disnake.Embed(
        title=draft.title or "Без заголовка",
        description=draft.body or "—",
        color=_draft_color(draft),
    )
    embed.set_footer(
        text=f"Администрация семьи • {author.display_name}",
        icon_url=author.display_avatar.url,
    )
    if image_file is not None:
        embed.set_image(url=f"attachment://{image_file.filename}")
    return embed


def _make_link_button(data: dict, row: int) -> disnake.ui.Button:
    return disnake.ui.Button(
        label=(data.get("label") or "Ссылка")[:80],
        url=data["url"],
        style=disnake.ButtonStyle.link,
        emoji=data.get("emoji") or None,
        row=row,
    )


async def _render_editor(
    edit_fn,
    draft: NewsDraft,
    author: disnake.Member,
    *,
    content: str | None = None,
) -> None:
    """Общий перерисовщик редактора: пересобирает картинку, embed и панель заново и
    применяет через переданную функцию (edit_message / edit_original_message)."""
    image_file = await _resolve_image_file(draft.image)
    embed = _build_embed(draft, author, image_file)
    kwargs = {"content": content, "embed": embed, "view": NewsEditorView(draft), "attachments": []}
    if image_file is not None:
        kwargs["file"] = image_file
    await edit_fn(**kwargs)


# ---------------------------------------------------------------------------
# Главная панель редактора
# ---------------------------------------------------------------------------

class NewsEditorView(disnake.ui.View):
    """Эфемерная панель редактирования — видна только тому, кто её открыл. Ряды 0–2 —
    пользовательские кнопки-ссылки, ряд 3 — управление (текст/цвет/ссылки/публикация)."""

    def __init__(self, draft: NewsDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        for i, data in enumerate(draft.buttons[:MAX_LINK_BUTTONS]):
            self.add_item(_make_link_button(data, row=i // 5))

    @disnake.ui.button(label="Текст", emoji="✏️", style=disnake.ButtonStyle.secondary, row=3, custom_id="news_ctrl_text")
    async def text_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(NewsTextModal(self.draft, inter))

    @disnake.ui.button(label="Цвет", emoji="🎨", style=disnake.ButtonStyle.secondary, row=3, custom_id="news_ctrl_color")
    async def color_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(NewsColorModal(self.draft, inter))

    @disnake.ui.button(label="Кнопки-ссылки", emoji="🔗", style=disnake.ButtonStyle.secondary, row=3, custom_id="news_ctrl_links")
    async def links_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.edit_message(view=NewsLinksView(self.draft))

    @disnake.ui.button(label="Опубликовать", emoji="✅", style=disnake.ButtonStyle.primary, row=3, custom_id="news_ctrl_publish")
    async def publish_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not self.draft.title.strip() and not self.draft.body.strip():
            await inter.response.send_message(
                "❌ Сначала заполните заголовок и текст новости — кнопка «✏️ Текст».", ephemeral=True
            )
            return

        await inter.response.defer()

        image_file = await _resolve_image_file(self.draft.image)
        embed = _build_embed(self.draft, inter.author, image_file)
        link_buttons = [_make_link_button(d, row=i // 5) for i, d in enumerate(self.draft.buttons[:MAX_LINK_BUTTONS])]

        live_view = None
        if link_buttons:
            live_view = disnake.ui.View(timeout=None)
            for b in link_buttons:
                live_view.add_item(b)

        send_kwargs = {"embed": embed, "view": live_view}
        if image_file is not None:
            send_kwargs["file"] = image_file

        await self.draft.channel.send(**send_kwargs)
        status = f"✅ Новость опубликована в {self.draft.channel.mention}."

        await inter.edit_original_message(content=status, embeds=[], attachments=[], view=None)


# ---------------------------------------------------------------------------
# Текст и цвет
# ---------------------------------------------------------------------------

class NewsTextModal(disnake.ui.Modal):
    def __init__(self, draft: NewsDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Заголовок новости",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=100,
                value=draft.title[:100] or None,
            ),
            disnake.ui.TextInput(
                label="Текст новости",
                custom_id="body",
                style=disnake.TextInputStyle.paragraph,
                max_length=3500,
                value=draft.body[:4000] or None,
            ),
        ]
        super().__init__(title="Текст новости", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        self.draft.title = inter.text_values["title"].strip()
        self.draft.body = inter.text_values["body"].strip()
        await _render_editor(self.panel_interaction.edit_original_message, self.draft, inter.author)


class NewsColorModal(disnake.ui.Modal):
    def __init__(self, draft: NewsDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        current = f"{draft.color:06X}" if draft.color is not None else None
        components = [
            disnake.ui.TextInput(
                label="Цвет полосы embed (HEX, например C9A227)",
                custom_id="color",
                style=disnake.TextInputStyle.short,
                max_length=7,
                required=False,
                value=current,
                placeholder="Оставьте пустым, чтобы вернуть цвет по умолчанию",
            ),
        ]
        super().__init__(title="Цвет новости", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["color"].strip().lstrip("#")
        if raw.lower().startswith("0x"):
            raw = raw[2:]

        if not raw:
            self.draft.color = None
        else:
            try:
                value = int(raw, 16)
                if not (0 <= value <= 0xFFFFFF):
                    raise ValueError
            except ValueError:
                await inter.response.send_message(
                    "❌ Не похоже на HEX-цвет. Пример: `C9A227` или `#C9A227`.", ephemeral=True
                )
                return
            self.draft.color = value

        await inter.response.defer(ephemeral=True)
        await _render_editor(self.panel_interaction.edit_original_message, self.draft, inter.author)


# ---------------------------------------------------------------------------
# Кнопки-ссылки
# ---------------------------------------------------------------------------

class NewsLinksView(disnake.ui.View):
    def __init__(self, draft: NewsDraft):
        super().__init__(timeout=1200)
        self.draft = draft
        if draft.buttons:
            self.add_item(NewsRemoveButtonSelect(draft))

    @disnake.ui.button(label="Добавить кнопку", emoji="➕", style=disnake.ButtonStyle.secondary, row=1, custom_id="news_links_add")
    async def add_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if len(self.draft.buttons) >= MAX_LINK_BUTTONS:
            await inter.response.send_message(f"❌ Уже добавлено максимум кнопок ({MAX_LINK_BUTTONS}).", ephemeral=True)
            return
        await inter.response.send_modal(NewsAddButtonModal(self.draft, inter))

    @disnake.ui.button(label="Назад", emoji="⬅️", style=disnake.ButtonStyle.secondary, row=1, custom_id="news_links_back")
    async def back_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.edit_message(view=NewsEditorView(self.draft))


class NewsRemoveButtonSelect(disnake.ui.StringSelect):
    def __init__(self, draft: NewsDraft):
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
        await inter.response.edit_message(view=NewsLinksView(self.draft))


class NewsAddButtonModal(disnake.ui.Modal):
    def __init__(self, draft: NewsDraft, panel_interaction: disnake.MessageInteraction):
        self.draft = draft
        self.panel_interaction = panel_interaction
        components = [
            disnake.ui.TextInput(
                label="Текст на кнопке",
                custom_id="label",
                style=disnake.TextInputStyle.short,
                max_length=80,
            ),
            disnake.ui.TextInput(
                label="Ссылка (начинается с http:// или https://)",
                custom_id="url",
                style=disnake.TextInputStyle.short,
                max_length=512,
            ),
            disnake.ui.TextInput(
                label="Эмодзи (необязательно)",
                custom_id="emoji",
                style=disnake.TextInputStyle.short,
                max_length=10,
                required=False,
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
            await self.panel_interaction.edit_original_message(view=NewsLinksView(self.draft))
        except disnake.HTTPException as exc:
            self.draft.buttons.remove(entry)
            await inter.followup.send(
                f"❌ Discord отклонил кнопку (возможно, некорректное эмодзи): {exc}", ephemeral=True
            )
            return
        await inter.followup.send("✅ Кнопка добавлена.", ephemeral=True)


# ---------------------------------------------------------------------------
# Слэш-команда
# ---------------------------------------------------------------------------

class News(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="news",
        description="Открыть редактор новости от лица администрации семьи",
        default_member_permissions=ADMIN_PERMS,
    )
    async def news(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel | None = commands.Param(
            default=None, description="Канал публикации (по умолчанию — канал новостей)"
        ),
        image: disnake.Attachment | None = commands.Param(
            default=None, description="Баннер новости (необязательно, иначе используется картинка из /img)"
        ),
    ):
        target_channel = channel or inter.guild.get_channel(config.get("channels.news", 0))
        if target_channel is None:
            await inter.response.send_message(
                "❌ Канал новостей не настроен и не указан вручную. Укажите канал параметром `channel`.",
                ephemeral=True,
            )
            return

        if image is not None and not _validate_image(image):
            await inter.response.send_message("❌ Приложенный файл не является изображением.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        draft = NewsDraft(target_channel, image)
        image_file = await _resolve_image_file(draft.image)
        embed = _build_embed(draft, inter.author, image_file)
        kwargs = {"embed": embed, "view": NewsEditorView(draft)}
        if image_file is not None:
            kwargs["file"] = image_file

        await inter.followup.send(
            "👀 **Редактор новости — видно только вам.** Заполните текст, при желании добавьте "
            "кнопки-ссылки и цвет, затем нажмите «Опубликовать».",
            ephemeral=True,
            **kwargs,
        )


def setup(bot: commands.InteractionBot):
    bot.add_cog(News(bot))
