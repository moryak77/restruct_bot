from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config
from core.storage import redux_store

MAX_KEY_LENGTH = 40

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

# Свой набор кастомных иконок (Tabler Icons, тот же серый тон #B5BAC1, что у остального бота).
_REDUX_ICON_IDS = {
    "tags": 1538632148943118426,
    "info": 1538632155150688336,
    "flame": 1538632160569856104,
    "users": 1538632035755630603,
    "bolt": 1538632166659854398,
    "target": 1538632172947243091,
}
REDUX_EMOJI = {key: disnake.PartialEmoji(name=f"role_{key}", id=emoji_id) for key, emoji_id in _REDUX_ICON_IDS.items()}

# Разделы каталога. Чтобы добавить ещё один — добавьте сюда запись и соответствующий
# блок в config.json (тот же формат, что у "redux"/"optimization"/"gunpacks": role_id + options[]).
CATEGORIES: list[dict] = [
    {"config_key": "redux", "title": "Редуксы", "emoji": REDUX_EMOJI["flame"], "select_id": "redux_select"},
    {"config_key": "optimization", "title": "Оптимизация", "emoji": REDUX_EMOJI["bolt"], "select_id": "optimization_select"},
    {"config_key": "gunpacks", "title": "Ганпаки", "emoji": REDUX_EMOJI["target"], "select_id": "gunpacks_select"},
]


def _load_catalog() -> dict:
    """Пункты каталога живут в redux_store (редактируются командой /redux adm), а не в
    config.json — тот теперь используется только для role_id (это инфраструктурная
    настройка, а не контент каталога). При первом запуске после обновления переносит
    то, что раньше лежало в config.json, чтобы не потерять уже добавленные пункты."""
    data = redux_store.load()
    if "categories" not in data:
        data["categories"] = {
            category["config_key"]: list(config.get(f"{category['config_key']}.options", []) or [])
            for category in CATEGORIES
        }
        redux_store.save(data)
    return data


def _options(config_key: str) -> list[dict]:
    data = _load_catalog()
    return [o for o in data["categories"].get(config_key, []) if o.get("key") and o.get("label")]


def _role_id(config_key: str) -> int | None:
    return config.get(f"{config_key}.role_id")


def _add_entry(config_key: str, label: str, description: str, video_url: str, download_url: str) -> dict:
    data = _load_catalog()
    bucket = data["categories"].setdefault(config_key, [])

    key = label.lower().replace(" ", "_")[:MAX_KEY_LENGTH]
    if any(o["key"] == key for o in bucket):
        key = f"{key}_{len(bucket) + 1}"[:MAX_KEY_LENGTH]

    entry = {
        "key": key,
        "label": label,
        "description": description or "",
        "video_url": video_url or "",
        "download_url": download_url or "",
    }
    bucket.append(entry)
    redux_store.save(data)
    return entry


def _build_redux_embed(guild: disnake.Guild | None) -> disnake.Embed:
    embed = base_embed(
        f"{REDUX_EMOJI['flame']} Каталог модов",
        "Нажмите на кнопку нужного раздела — откроется меню, в котором можно выбрать "
        "конкретный пункт. Бот пришлёт лично вам описание, видео-превью и кнопку для "
        "скачивания. Ставить можно сколько угодно (по очереди).",
        panel_key="redux",
    )
    embed.add_field(
        name=f"{REDUX_EMOJI['info']} Как это работает",
        value=(
            "**1.** Нажмите кнопку раздела ниже — откроется меню с пунктами внутри.\n"
            "**2.** Выберите нужный пункт — описание, видео и ссылка на скачивание придут только вам.\n"
            "**3.** Если у раздела есть роль доступа — она выдастся автоматически при первом выборе."
        ),
        inline=False,
    )

    summary_lines = [
        f"{category['emoji']} **{category['title']}** — {len(_options(category['config_key']))} шт."
        for category in CATEGORIES
    ]
    embed.add_field(name=f"{REDUX_EMOJI['tags']} Разделы", value="\n".join(summary_lines), inline=False)

    if guild is not None:
        role_to_titles: dict[int, list[str]] = {}
        for category in CATEGORIES:
            role_id = _role_id(category["config_key"])
            if role_id:
                role_to_titles.setdefault(role_id, []).append(category["title"])

        access_lines = []
        for role_id, titles in role_to_titles.items():
            role = guild.get_role(role_id)
            if role is not None:
                access_lines.append(
                    f"{role.mention} — доступ к: {', '.join(titles)} · **{len(role.members)}** участников"
                )
        if access_lines:
            embed.add_field(name=f"{REDUX_EMOJI['users']} Роли доступа", value="\n".join(access_lines), inline=False)

    return embed


async def _refresh_redux_panel(guild: disnake.Guild) -> None:
    data = redux_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    embed = _build_redux_embed(guild)
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=RedoxPanelView())
    except disnake.HTTPException:
        pass


class CatalogSelect(disnake.ui.StringSelect):
    def __init__(self, category: dict):
        self.category = category
        options_cfg = _options(category["config_key"])
        options = [
            disnake.SelectOption(label=o["label"][:100], value=o["key"], emoji=category["emoji"])
            for o in options_cfg[:25]
        ]
        super().__init__(
            placeholder=f"{category['title']} — выберите пункт"[:150],
            options=options,
            custom_id=category["select_id"],
        )

    async def callback(self, inter: disnake.MessageInteraction):
        config_key = self.category["config_key"]
        entry = next((o for o in _options(config_key) if o["key"] == self.values[0]), None)
        if entry is None:
            await inter.response.send_message("❌ Этот пункт больше не найден в конфиге.", ephemeral=True)
            return

        embed = base_embed(
            f"{self.category['emoji']} {entry['label']}",
            entry.get("description") or "Описание пока не добавлено.",
        )
        if entry.get("download_url"):
            embed.add_field(name="⬇️ Скачивание", value=f"[Ссылка на скачивание]({entry['download_url']})", inline=True)
        if entry.get("video_url"):
            embed.add_field(name="🎬 Видео", value=f"[Смотреть превью]({entry['video_url']})", inline=True)

        view = disnake.ui.View()
        if entry.get("download_url"):
            view.add_item(disnake.ui.Button(label="Скачать", style=disnake.ButtonStyle.link, url=entry["download_url"]))
        if entry.get("video_url"):
            view.add_item(disnake.ui.Button(label="Видео", style=disnake.ButtonStyle.link, url=entry["video_url"]))

        content = entry["video_url"] if entry.get("video_url") else None
        await inter.response.edit_message(content=content, embed=embed, view=view if view.children else None)

        role_id = _role_id(config_key)
        if not role_id:
            return
        role = inter.guild.get_role(role_id)
        if role is None:
            await inter.followup.send(
                f"⚠️ Роль доступа не найдена на сервере (проверьте {config_key}.role_id в config.json).",
                ephemeral=True,
            )
            return
        if role not in inter.author.roles:
            try:
                await inter.author.add_roles(role, reason=f"Выбрал «{entry['label']}» ({self.category['title']})")
                await _refresh_redux_panel(inter.guild)
            except disnake.Forbidden:
                await inter.followup.send(
                    f"⚠️ Не удалось выдать роль {role.mention} — роль бота ниже неё в иерархии ролей сервера.",
                    ephemeral=True,
                )


class CategoryPickerView(disnake.ui.View):
    def __init__(self, category: dict):
        super().__init__(timeout=180)
        self.add_item(CatalogSelect(category))


class CategoryButton(disnake.ui.Button):
    def __init__(self, category: dict):
        self.category = category
        super().__init__(
            label=category["title"],
            emoji=category["emoji"],
            style=disnake.ButtonStyle.secondary,
            custom_id=f"catalog_open_{category['config_key']}",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if not _options(self.category["config_key"]):
            await inter.response.send_message("Этот раздел пока пуст.", ephemeral=True)
            return
        await inter.response.send_message(
            f"{self.category['emoji']} Выберите пункт из раздела «{self.category['title']}»:",
            view=CategoryPickerView(self.category),
            ephemeral=True,
        )


class RedoxPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for category in CATEGORIES:
            self.add_item(CategoryButton(category))


# ---------------------------------------------------------------------------
# Администрирование: добавление пунктов в каталог
# ---------------------------------------------------------------------------

class AddEntryModal(disnake.ui.Modal):
    def __init__(self, category: dict):
        self.category = category
        components = [
            disnake.ui.TextInput(
                label="Название",
                custom_id="label",
                style=disnake.TextInputStyle.short,
                max_length=80,
            ),
            disnake.ui.TextInput(
                label="Описание",
                custom_id="description",
                style=disnake.TextInputStyle.paragraph,
                max_length=1000,
                required=False,
            ),
            disnake.ui.TextInput(
                label="Ссылка на скачивание",
                custom_id="download_url",
                style=disnake.TextInputStyle.short,
                max_length=300,
                placeholder="Необязательно — оставьте пустым, если пока нет",
                required=False,
            ),
            disnake.ui.TextInput(
                label="Ссылка на видео-превью",
                custom_id="video_url",
                style=disnake.TextInputStyle.short,
                max_length=300,
                placeholder="Необязательно — оставьте пустым, если пока нет",
                required=False,
            ),
        ]
        super().__init__(title=f"Добавить: {category['title']}"[:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        label = inter.text_values["label"].strip()
        if not label:
            await inter.response.send_message("❌ Укажите название.", ephemeral=True)
            return

        entry = _add_entry(
            self.category["config_key"],
            label,
            inter.text_values["description"].strip(),
            inter.text_values["video_url"].strip(),
            inter.text_values["download_url"].strip(),
        )

        await inter.response.send_message(
            f"✅ Добавлено в «{self.category['title']}»: **{entry['label']}**.", ephemeral=True
        )
        await _refresh_redux_panel(inter.guild)


class AdminCategoryButton(disnake.ui.Button):
    def __init__(self, category: dict):
        self.category = category
        super().__init__(
            label=f"Добавить: {category['title']}",
            emoji=category["emoji"],
            style=disnake.ButtonStyle.secondary,
            custom_id=f"catalog_adm_add_{category['config_key']}",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        await inter.response.send_modal(AddEntryModal(self.category))


class RedoxAdminView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for category in CATEGORIES:
            self.add_item(AdminCategoryButton(category))


class Redux(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="redux",
        description="Каталог редуксов, оптимизаций и ганпаков",
        default_member_permissions=ADMIN_PERMS,
    )
    async def redux(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @redux.sub_command(name="show", description="Опубликовать каталог модов (только для персонала)")
    async def redux_show(self, inter: disnake.ApplicationCommandInteraction):
        embed = _build_redux_embed(inter.guild)
        message = await send_panel(inter, embed, view=RedoxPanelView(), panel_key="redux")

        data = redux_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        redux_store.save(data)

    @redux.sub_command(name="adm", description="Добавить пункт в каталог — редукс, оптимизацию или ганпак (только для персонала)")
    async def redux_adm(self, inter: disnake.ApplicationCommandInteraction):
        embed = base_embed(
            f"{REDUX_EMOJI['tags']} Добавление в каталог",
            "Выберите раздел, в который хотите добавить пункт — откроется окно с полями "
            "«Название», «Описание», «Ссылка на скачивание» и «Ссылка на видео». Обязательно "
            "только название — остальные поля можно оставить пустыми, если пока нечего указать.",
        )
        await inter.response.send_message(embed=embed, view=RedoxAdminView(), ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Redux(bot))


PERSISTENT_VIEWS = [lambda: RedoxPanelView(), lambda: RedoxAdminView()]
