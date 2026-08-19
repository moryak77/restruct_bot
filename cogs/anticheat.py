from __future__ import annotations

from pathlib import Path

import disnake
from disnake.ext import commands

from core.branding import base_embed
from core.icons import icon_tag

ASSETS_DIR = Path(__file__).resolve().parent.parent / "data" / "anticheat_checker"
EXE_PATH = ASSETS_DIR / "Restruct - Checker.exe"
CONFIG_PATH = ASSETS_DIR / "flagged_processes.json"


def _build_embeds() -> list[disnake.Embed]:
    intro = base_embed(
        f"{icon_tag('shield')} Restruct - Checker — проверка на читы",
        (
            "Локальная программа для проверки игрока на читы во время очной модерации "
            "(созвон/скриншер). Ничего не отправляет в сеть, не работает в фоне и не "
            "запускается скрытно — весь результат виден только на экране того, кто её "
            "запустил.\n\n"
            f"{icon_tag('alert')} **Это не полноценный античит.** Ни один локальный "
            "сканер не даёт 100% обнаружения — умелый чит может переименовать себя или "
            "спрятаться. Программа — подспорье при очной проверке, финальное решение "
            "всегда принимает администратор, а не программа."
        ),
    )

    howto = base_embed(f"{icon_tag('help')} Как проходит проверка — по шагам")
    howto.description = (
        "**1.** Администратор открывает с игроком созвон или запрашивает демонстрацию "
        "экрана (скриншер) — весь дальнейший процесс проходит у админа на глазах.\n"
        "**2.** Игроку отправляются два файла из этого сообщения: `Restruct - Checker.exe` "
        "и `flagged_processes.json` — оба должны лежать в одной папке.\n"
        "**3.** Игрок запускает `.exe` **у себя на экране, который видит администратор**, "
        "и по очереди проходит разделы приложения.\n"
        "**4.** Администратор сам смотрит на результат в каждом разделе и решает, что "
        "делать дальше — программа только показывает совпадения, вывод делает человек.\n"
        "**5.** При необходимости результат можно скопировать кнопкой «Скопировать отчёт» "
        "и прислать текстом в тикет — на компьютере игрока также сами собой остаются "
        "файлы-логи каждой проверки в папке `logs` рядом с программой."
    )

    sections = base_embed(f"{icon_tag('tool')} Разделы приложения — что считается подозрительным")
    sections.add_field(
        name=f"{icon_tag('shield')} Проверка",
        value=(
            "Сверяет запущенные процессы со списком известных читерских инструментов и "
            "смотрит DLL, загруженные в саму игру. **Подозрительно:** процесс из списка, "
            "или модуль в игре, подгруженный из Temp/Downloads/Desktop вместо папки игры."
        ),
        inline=False,
    )
    sections.add_field(
        name="🗂️ Все процессы",
        value=(
            "Полный список вообще всех процессов компьютера. Клик по любому — детали и "
            "кнопка проверки цифровой подписи. **Подозрительно:** неизвестный процесс без "
            "подписи, запущенный из странного места (не Program Files/System32)."
        ),
        inline=False,
    )
    sections.add_field(
        name="🗄️ Скан диска",
        value=(
            "Полный обход **каждого файла на каждом диске**, включая System32 — не "
            "выборочно, а буквально всё. **Подозрительно:** любой файл, чьё имя совпадает "
            "со списком читерских программ, найденный где угодно на диске."
        ),
        inline=False,
    )
    sections.add_field(
        name="🗑️ Корзина",
        value=(
            "Недавно удалённые файлы со всех дисков. **Подозрительно:** файл из списка, "
            "удалённый совсем недавно (особенно прямо перед проверкой) — самый частый "
            "приём, чтобы спрятать чит."
        ),
        inline=False,
    )
    sections.add_field(
        name="🕘 Активность",
        value=(
            "Что запускалось недавно (Windows Prefetch) и какие пути/команды вводились "
            "вручную (реестр). **Подозрительно:** запись о запуске программы из списка, "
            "даже если сам файл уже удалён или переименован."
        ),
        inline=False,
    )
    sections.add_field(
        name="📄 Логи · ℹ️ О программе",
        value="Логи — история всех проверок файлами на диске. О программе — краткая справка внутри самого приложения.",
        inline=False,
    )

    setup = base_embed(f"{icon_tag('settings')} Настройка списка")
    setup.description = (
        "Файл `flagged_processes.json` — редактируемый список того, что считается "
        "подозрительным (имена процессов, файлов, папка игры и т.д.). Открывается любым "
        "текстовым редактором. Актуальные названия читерских программ под GTA5RP знает "
        "команда модерации по своей практике — этот список стоит пополнять по факту "
        "реальных случаев, а не полагаться только на встроенные примеры."
    )

    return [intro, howto, sections, setup]


class AntiCheat(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="anticheat", description="Проверка на читы (GTA5RP)")
    async def anticheat(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @anticheat.sub_command(name="show", description="Опубликовать инструкцию и файлы программы проверки на читы (только для персонала)")
    async def anticheat_show(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав для публикации этой панели.", ephemeral=True)
            return

        if not EXE_PATH.exists() or not CONFIG_PATH.exists():
            await inter.response.send_message(
                "❌ Файлы программы не найдены на сервере бота — обратитесь к разработчику.", ephemeral=True
            )
            return

        # Лимит вложений зависит от уровня буста конкретного сервера (10-100 МБ) — не хардкодим,
        # а спрашиваем у Discord напрямую, чтобы не упасть с 413 при переезде на менее забустенный
        # сервер (.exe почти 19 МБ — легко перестаёт помещаться).
        total_size = EXE_PATH.stat().st_size + CONFIG_PATH.stat().st_size
        limit = inter.guild.filesize_limit
        if total_size > limit:
            await inter.response.send_message(
                f"❌ Файлы программы ({total_size / 1_048_576:.1f} МБ суммарно) больше лимита вложений "
                f"этого сервера ({limit / 1_048_576:.0f} МБ — зависит от уровня буста). Забустите сервер "
                "до нужного уровня либо выложите `Restruct - Checker.exe` во внешнее хранилище (Google "
                "Диск, Telegram и т.п.) и опубликуйте ссылку вместо файла.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)
        files = [disnake.File(EXE_PATH), disnake.File(CONFIG_PATH)]
        try:
            await inter.channel.send(embeds=_build_embeds(), files=files)
        except disnake.HTTPException as e:
            await inter.followup.send(f"❌ Не удалось опубликовать файлы: {e}", ephemeral=True)
            return

        await inter.followup.send("✅ Панель опубликована в этом канале.", ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(AntiCheat(bot))
