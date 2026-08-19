from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import disnake
from disnake.ext import commands, tasks

from core.config import DISCORD_TOKEN, TEST_GUILDS

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("restruct-bot")

LOCK_PATH = Path(__file__).resolve().parent / "bot.lock"
_lock_file = None  # держим файл открытым на всё время жизни процесса — так ОС сама снимет
                    # блокировку, если процесс убьют жёстко (крашнется, прибьют из диспетчера и т.д.)


def _acquire_single_instance_lock() -> None:
    """Не даёт запустить второй процесс бота поверх уже работающего — если это не отследить,
    оба экземпляра будут висеть на одном токене и дёргать одни и те же голосовые каналы/данные,
    что и выглядит как случайные подвисания и тайм-ауты interactions."""
    global _lock_file
    try:
        import msvcrt
    except ImportError:
        return  # не Windows — блокировку пропускаем, не критично для этого проекта

    _lock_file = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error(
            "Бот уже запущен в другом процессе (файл %s занят). "
            "Закройте старый процесс (Диспетчер задач → python.exe) перед повторным запуском.",
            LOCK_PATH,
        )
        _lock_file.close()
        sys.exit(1)

EXTENSIONS = [
    "cogs.autorole",
    "cogs.welcome",
    "cogs.moderation",
    "cogs.announce",
    "cogs.roles",
    "cogs.cars",
    "cogs.afk",
    "cogs.images",
    "cogs.tickets",
    "cogs.warns",
    "cogs.voice",
    "cogs.vzp",
    "cogs.news",
    "cogs.bank",
    "cogs.contracts",
    "cogs.music",
    "cogs.redux",
    "cogs.rules",
    "cogs.shop",
    "cogs.anticheat",
    "cogs.stats",
    "cogs.help",
]

intents = disnake.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True  # нужно, чтобы читать текст сообщений из канала-трубы вебхука ЮKassa (cogs.shop)
intents.presences = True  # нужно для подсчёта участников онлайн (cogs.stats)

STATUS_INTERVAL_SECONDS = 60

# Ротация статуса бота — каждый пункт живёт по STATUS_INTERVAL_SECONDS, дальше по кругу.
# Используем Custom Status (тот же тип, что и у обычных пользователей) с текстом глагола
# прямо в строке — обычные Activity-типы (Playing/Watching/...) в компактном списке
# участников у ботов не всегда дорисовывают приставку "Смотрит"/"Играет" на клиенте.
STATUSES: list[str] = [
    "🎮 Играет в ВЗП со слонами",
    "👁️ Смотрит за контрактами",
    "🎧 Слушает музыку в войсе",
    "🎫 Смотрит за тикетами поддержки",
]

bot = commands.InteractionBot(
    intents=intents,
    test_guilds=TEST_GUILDS,
    activity=disnake.CustomActivity(name=STATUSES[0]),
)

_views_registered = False
_status_loop_started = False
_status_index = 0


@tasks.loop(seconds=STATUS_INTERVAL_SECONDS)
async def rotate_status() -> None:
    global _status_index
    text = STATUSES[_status_index % len(STATUSES)]
    _status_index += 1
    await bot.change_presence(activity=disnake.CustomActivity(name=text))


@bot.event
async def on_ready():
    global _views_registered, _status_loop_started
    log.info("Бот запущен как %s (ID: %s)", bot.user, bot.user.id)

    if not _status_loop_started:
        rotate_status.start()
        _status_loop_started = True
        log.info("Ротация статуса запущена.")

    if not _views_registered:
        for extension_name in EXTENSIONS:
            module = bot.extensions.get(extension_name)
            for factory in getattr(module, "PERSISTENT_VIEWS", []):
                bot.add_view(factory())
        _views_registered = True
        log.info("Persistent-views зарегистрированы.")

        # Три независимые операции очистки/обновления по каждой гильдии — раньше шли строго
        # последовательно (каждая свой цикл await по всем guilds), хотя друг от друга не
        # зависят. Запускаем параллельно через asyncio.gather — на серверах с несколькими
        # гильдиями старт бота становится быстрее.
        startup_refreshes: list[tuple[str, object]] = []
        tickets_module = bot.extensions.get("cogs.tickets")
        refresh = getattr(tickets_module, "_refresh_active_tickets_list", None)
        if refresh is not None:
            startup_refreshes.append(("Список активных заявок обновлён и очищен от протухших записей.", refresh))

        shop_module = bot.extensions.get("cogs.shop")
        refresh_quotes = getattr(shop_module, "_refresh_quote_list", None)
        if refresh_quotes is not None:
            startup_refreshes.append(("Список заказов «по запросу» обновлён и очищен от протухших записей.", refresh_quotes))

        voice_module = bot.extensions.get("cogs.voice")
        prune_voice = getattr(voice_module, "_prune_empty_voice_channels", None)
        if prune_voice is not None:
            startup_refreshes.append(("Пустые приватные голосовые каналы очищены.", prune_voice))

        async def _run_for_all_guilds(coro) -> None:
            for guild in bot.guilds:
                try:
                    await coro(guild)
                except disnake.HTTPException:
                    pass

        await asyncio.gather(*(_run_for_all_guilds(coro) for _, coro in startup_refreshes))
        for message, _ in startup_refreshes:
            log.info(message)

    if TEST_GUILDS:
        log.info("Слэш-команды синхронизируются локально на сервер(а): %s", TEST_GUILDS)
    else:
        log.info("Слэш-команды синхронизируются глобально (обновление может занять до часа).")


def main() -> None:
    _acquire_single_instance_lock()

    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN не найден. Скопируйте .env.example в .env и укажите токен бота."
        )

    for extension_name in EXTENSIONS:
        bot.load_extension(extension_name)
        log.info("Загружен модуль: %s", extension_name)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
