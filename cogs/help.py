from __future__ import annotations

import disnake
from disnake.ext import commands, tasks

from core.branding import base_embed, panel_file_kwargs
from core.config import config
from core.icons import icon, icon_tag
from core.storage import help_store

REFRESH_SECONDS = config.get("help.refresh_seconds", 43200) or 43200  # 12 часов по умолчанию

ACCESS_ALL = f"{icon_tag('users')} Все участники"
ACCESS_STAFF = f"{icon_tag('lock')} Управление сервером"
ACCESS_MOD = f"{icon_tag('gavel')} Модерация участников"

# Уровни доступа для кнопки «Мои команды» на панели «/help show» — бот сам определяет
# самую высокую роль нажавшего и показывает только доступные ей команды (плюс всё, что
# доступно всем — ACCESS_ALL всегда входит в набор, т.к. персонал и модерация тоже
# пользуются общими командами). Порядок в списке — от старшей роли к младшей.
ACCESS_LEVELS: list[tuple[str, str]] = [
    ("staff", ACCESS_STAFF),
    ("mod", ACCESS_MOD),
    ("all", ACCESS_ALL),
]


def _member_access_key(member: disnake.Member) -> str:
    """Самая высокая роль участника по фактическим правам на сервере — так же, как
    is_staff/MOD_PERMS определяют доступ к самим командам в других когах."""
    perms = member.guild_permissions
    if perms.manage_guild:
        return "staff"
    if perms.moderate_members:
        return "mod"
    return "all"


# (заголовок раздела, [(команда, описание, доступ), ...])
# Порядок команд/описаний ведётся вручную — держите в актуальном состоянии при
# добавлении новых слэш-команд, это не генерируется автоматически из кода.
HELP_SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (f"{icon_tag('ticket')} Тикеты и поддержка", [
        ("/ticket", "Опубликовать общую панель поддержки (кнопку открытия тикета видят все)", ACCESS_STAFF),
        ("/curator", "Опубликовать панель тикетов к куратору академии", ACCESS_STAFF),
        ("/recruit", "Опубликовать панель подачи заявок: в семью, на ВЗП", ACCESS_STAFF),
        ("/moderation", "Опубликовать в этом канале список активных заявок", ACCESS_STAFF),
    ]),
    (f"{icon_tag('users')} Приветствие", [
        ("/welcome preview", "Показать себе, как выглядит приветствие новых участников", ACCESS_STAFF),
    ]),
    (f"{icon_tag('car')} Автопарк", [
        ("/cars show", "Показать панель автопарка семьи", ACCESS_ALL),
        ("/cars adm", "Добавить или удалить машину из автопарка", ACCESS_STAFF),
    ]),
    (f"{icon_tag('bank')} Экономика", [
        ("/bank", "Показать панель банка семьи (баланс, пополнение/списание)", ACCESS_STAFF),
        ("/history_bank", "Посмотреть историю операций банка семьи", ACCESS_ALL),
        ("/contract", "Показать панель контрактов семьи", ACCESS_ALL),
    ]),
    (f"{icon_tag('coin')} Магазин услуг", [
        ("/shop show", "Опубликовать каталог платных услуг семьи", ACCESS_STAFF),
        ("/shop adm", "Добавить, изменить или удалить услугу в магазине", ACCESS_STAFF),
        ("/shop payments", "Заказы, ожидающие оплаты — подтвердить вручную при сбое", ACCESS_STAFF),
        ("/shop moderation", "Опубликовать в этом канале панель модерации заказов «по сложности»", ACCESS_STAFF),
    ]),
    (f"{icon_tag('package')} Каталог модов", [
        ("/redux show", "Опубликовать каталог редуксов, оптимизаций и ганпаков", ACCESS_STAFF),
        ("/redux adm", "Добавить пункт в каталог (название, описание, ссылки)", ACCESS_STAFF),
    ]),
    (f"{icon_tag('gavel')} Модерация", [
        ("/warn", "Выдать участнику предупреждение с указанием причины", ACCESS_MOD),
        ("/unwarn", "Снять с участника одно из активных предупреждений", ACCESS_MOD),
        ("/warns", "Посмотреть активные предупреждения участника", ACCESS_ALL),
        ("ПКМ на участнике → «Выдать предупреждение»", "Контекстное меню — то же самое, что /warn", ACCESS_MOD),
    ]),
    (f"{icon_tag('key')} Голосовые каналы", [
        ("/voice", "Опубликовать панель создания приватных голосовых каналов", ACCESS_STAFF),
    ]),
    (f"{icon_tag('send')} Музыка", [
        ("/music", "Показать музыкальный плеер (доступен всем в одном голосовом с ботом)", ACCESS_ALL),
    ]),
    (f"{icon_tag('alert')} Новости", [
        ("/news", "Опубликовать новость от лица администрации семьи", ACCESS_STAFF),
    ]),
    (f"{icon_tag('clipboard')} ВЗП", [
        ("/vzp", "Запустить сбор сотрудников по должностям на ВЗП", ACCESS_STAFF),
    ]),
    (f"{icon_tag('moon')} AFK", [
        ("/afk", "Показать панель сотрудников, находящихся в AFK или отпуске", ACCESS_ALL),
    ]),
    (f"{icon_tag('settings')} Оформление", [
        ("/img", "Сменить картинку указанной панели (баннер)", ACCESS_STAFF),
    ]),
    (f"{icon_tag('shield')} Правила", [
        ("/rules show", "Опубликовать панель с правилами сервера", ACCESS_STAFF),
        ("/rules edit", "Изменить текст правил", ACCESS_STAFF),
    ]),
    (f"{icon_tag('tool')} Проверка на читы", [
        ("/anticheat show", "Опубликовать инструкцию и файлы программы проверки на читы", ACCESS_STAFF),
    ]),
    (f"{icon_tag('pending')} Статистика", [
        ("/stats show", "Опубликовать самообновляющуюся панель статистики сервера", ACCESS_STAFF),
        ("/stats refresh", "Обновить панель статистики прямо сейчас, не дожидаясь таймера", ACCESS_STAFF),
    ]),
    (f"{icon_tag('help')} Справка", [
        ("/help me", "Показать доступные вам команды", ACCESS_ALL),
        ("/help show", "Опубликовать в канале панель-инструкцию (с автообновлением)", ACCESS_STAFF),
    ]),
]


def _build_help_intro_embed(published: bool = False) -> disnake.Embed:
    family = config.get("family_name", "RESTRUCT")
    total_commands = sum(len(commands_list) for _, commands_list in HELP_SECTIONS)
    total_sections = len(HELP_SECTIONS)

    embed = base_embed(
        f"{icon_tag('help')} Команды бота семьи {family}",
        (
            f"Бот семьи {family} управляет автопарком, банком, контрактами, тикетами, "
            "магазином услуг и другими разделами через слэш-команды — часть из них доступна "
            "всем, часть только персоналу или модерации.\n\n"
            "Большинство команд публикуют **панель** с кнопками — самими кнопками (открыть "
            "тикет, забрать заказ и т.д.) после публикации может пользоваться уже тот, кому "
            "это разрешено логикой конкретной панели, а не эта команда.\n\n"
            f"{icon_tag('hand')} Нажмите кнопку ниже — бот сам определит вашу роль и покажет "
            "только доступные вам команды."
        ),
        panel_key="help",
    )
    embed.add_field(
        name=f"{icon_tag('tool')} Коротко о боте",
        value=(
            f"Всего в боте **{total_commands}** команд, объединённых в **{total_sections}** разделов.\n"
            f"{icon_tag('send')} Совет: начните вводить `/` в чате — Discord сам подскажет "
            "описание и аргументы любой команды бота, без похода сюда."
        ),
        inline=False,
    )
    if published:
        hours = max(REFRESH_SECONDS // 3600, 1)
        embed.set_footer(text=f"Обновляется каждые {hours} ч · Семья {family}")
    return embed


def _build_help_embeds_for_access(access_key: str) -> list[disnake.Embed]:
    """Тот же список команд, но отфильтрованный под конкретную роль — ACCESS_ALL входит
    в набор всегда, т.к. персонал и модерация тоже пользуются общими командами."""
    _, label = next(level for level in ACCESS_LEVELS if level[0] == access_key)
    allowed = {ACCESS_ALL}
    if access_key == "staff":
        allowed.add(ACCESS_STAFF)
    elif access_key == "mod":
        allowed.add(ACCESS_MOD)

    embed = base_embed(
        f"{icon_tag('check')} Ваши команды",
        f"Ваша роль на сервере: {label}. Ниже — слэш-команды, доступные вам.",
    )
    for title, commands_list in HELP_SECTIONS:
        filtered = [(name, desc) for name, desc, access in commands_list if access in allowed]
        if not filtered:
            continue
        value = "\n".join(f"**{name}** — {desc}" for name, desc in filtered)
        embed.add_field(name=title, value=value[:1024], inline=False)

    if not embed.fields:
        embed.description = "Для этой роли пока нет отдельных команд."

    return [embed]


class HelpMenuView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(
        label="Мои команды",
        emoji=icon("clipboard"),
        style=disnake.ButtonStyle.secondary,
        custom_id="help_my_commands",
    )
    async def my_commands_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        access_key = _member_access_key(inter.author)
        embeds = _build_help_embeds_for_access(access_key)
        await inter.response.send_message(embeds=embeds, ephemeral=True)


PERSISTENT_VIEWS = [HelpMenuView]


async def _refresh_help_panel(guild: disnake.Guild) -> None:
    data = help_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=_build_help_intro_embed(published=True))
    except disnake.HTTPException:
        pass


class Help(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def refresh_loop(self):
        data = help_store.load()
        if not data.get("auto_refresh", True):
            return
        guild_id = data.get("guild_id")
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        await _refresh_help_panel(guild)

    @refresh_loop.before_loop
    async def _before_refresh_loop(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(name="help", description="Команды бота")
    async def help(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @help.sub_command(name="me", description="Показать доступные вам команды бота")
    async def help_me(self, inter: disnake.ApplicationCommandInteraction):
        access_key = _member_access_key(inter.author)
        embeds = _build_help_embeds_for_access(access_key)
        await inter.response.send_message(embeds=embeds, ephemeral=True)

    @help.sub_command(
        name="show",
        description="Опубликовать в канале панель-инструкцию — обновляется сама каждые 12 часов (только персонал)",
    )
    async def help_show(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав для публикации этой панели.", ephemeral=True)
            return

        await inter.response.send_message("✅ Панель опубликована в этом канале.", ephemeral=True)
        message = await inter.channel.send(
            embed=_build_help_intro_embed(published=True), view=HelpMenuView(), **panel_file_kwargs("help")
        )

        data = help_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        data["guild_id"] = inter.guild_id
        data["auto_refresh"] = True
        help_store.save(data)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Help(bot))
