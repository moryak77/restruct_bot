from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Один выделенный поток-писатель на всё хранилище — запись на диск больше не блокирует
# event loop (единственный поток, в котором крутится вся остальная логика бота: heartbeat
# к Discord, ответы на interactions и т.д.). Именно один воркер, а не пул из нескольких —
# так задачи гарантированно выполняются в порядке отправки: с несколькими воркерами два
# save() одного файла могли бы завершиться на диске не в том порядке (OS-планировщик мог
# бы дописать более старое состояние позже нового). Пропускной способности одного потока
# с большим запасом хватает — файлы маленькие, пишутся не чаще нескольких раз в секунду.
_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jsonstore-writer")


class JsonStore:
    """Простое персистентное хранилище на базе одного JSON-файла.

    Распарсенный JSON держится в памяти: load() отдаёт deepcopy кэша (быстрее round-трипа
    через json.dumps/loads, но даёт ту же гарантию — каждый load() независим, мутировать
    результат до save() безопасно). save() остаётся синхронным для вызывающего кода (менять
    сигнатуру во всех ~20 когах не нужно), но сама запись на диск уходит в фоновый поток —
    event loop не ждёт файловый I/O."""

    def __init__(self, filename: str, default: Any):
        self._path = DATA_DIR / filename
        self._default = default
        self._cache: Any = None
        if not self._path.exists():
            self.save(default)

    def load(self) -> Any:
        if self._cache is None:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self._cache = copy.deepcopy(self._default)
        return copy.deepcopy(self._cache)

    def save(self, data: Any) -> None:
        self._cache = data
        # Замораживаем снимок для фонового потока — вызывающий код мог бы продолжить
        # мутировать `data` сразу после save(), это не должно повлиять на то, что реально
        # уйдёт на диск.
        _write_pool.submit(self._write_to_disk, copy.deepcopy(data))

    def _write_to_disk(self, data: Any) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


cars_store = JsonStore(
    "cars.json", {"next_id": 1, "cars": [], "panel_channel_id": None, "panel_message_id": None}
)
afk_store = JsonStore("afk.json", {"entries": []})
warns_store = JsonStore(
    "warns.json",
    {"next_id": 1, "warns": [], "level_role_ids": {}},  # level_role_ids: "1"/"2"/"3" -> role_id
)
tickets_store = JsonStore(
    "tickets.json",
    {"counter": 0, "open": {}, "active_list_channel_id": None, "active_list_message_id": None},
)
voice_store = JsonStore("voice.json", {"channels": {}})
vzp_store = JsonStore("vzp.json", {"sessions": {}})
bank_store = JsonStore(
    "bank.json",
    {"balance": 0, "next_id": 1, "transactions": [], "panel_channel_id": None, "panel_message_id": None},
)
contracts_store = JsonStore(
    "contracts.json",
    {"next_id": 1, "contracts": [], "panel_channel_id": None, "panel_message_id": None},
)
music_store = JsonStore("music.json", {"panel_channel_id": None, "panel_message_id": None})
redux_store = JsonStore("redux.json", {"panel_channel_id": None, "panel_message_id": None})
shop_store = JsonStore(
    "shop.json",
    {
        "services": [
            {
                "key": "design",
                "label": "DESIGN",
                "icon": "design",
                "price": 300,
                "description": "Превью, логотипы, оформление серверов и каналов, баннеры, иконки.",
            },
            {
                "key": "editor",
                "label": "EDITOR",
                "icon": "editor",
                "price": 500,
                "description": "Монтаж видео, нарезки, GIF-анимации, визуальные эффекты.",
            },
            {
                "key": "reklama",
                "label": "REKLAMA",
                "icon": "reklama",
                "price": 200,
                "description": "Реклама и коллаборации — размещение объявлений о вашем проекте/сервере.",
            },
            {
                "key": "bots",
                "label": "БОТЫ (Discord/Telegram)",
                "icon": "bots",
                "price": 1500,
                "description": "Разработка ботов под заказ — Discord и Telegram: модерация, тикеты, экономика, автоматизация и т.д.",
            },
        ],
        "panel_channel_id": None,
        "panel_message_id": None,
    },
)
payments_store = JsonStore("payments.json", {"orders": [], "next_id": 1})
stats_store = JsonStore(
    "stats.json", {"panel_channel_id": None, "panel_message_id": None, "guild_id": None}
)
help_store = JsonStore(
    "help.json", {"panel_channel_id": None, "panel_message_id": None, "guild_id": None, "auto_refresh": True}
)
rules_store = JsonStore(
    "rules.json",
    {
        "rules": [
            "Оскорбление других участников Discord-сервера/семьи, будь то прямым или косвенным образом.",
            "Публикация материалов грубого, насильственного характера, жестокости, а также призывов "
            "экстремистского толка.",
            "Дискриминация по любому признаку — расовому, национальному, гражданскому, половому, "
            "религиозному, возрастному, по инвалидности, роду занятий.",
            "Употребление уничижительных определений различных национальностей, народов и групп "
            "(например «пиндосы», «хохлы», «москали» и т.п.).",
            "Публикация сообщений, призывающих к суициду.",
            "Разглашение чьей бы то ни было персональной информации.",
            "Неадекватное поведение в голосовых/текстовых каналах.",
            "Любой вред участникам или серверу (в том числе DDoS-атаки).",
        ],
        "panel_channel_id": None,
        "panel_message_id": None,
    },
)
moderation_store = JsonStore(
    "moderation.json",
    {
        "next_id": 1,
        "temp_bans": {},  # str(user_id) -> {guild_id, unban_at, reason, banned_by}
        "requests": {},   # str(request_id) -> заявка на наказание (см. cogs/moderation.py)
    },
)
announce_store = JsonStore(
    "announce.json",
    {"title": "", "body": "", "messages": {}},  # messages: str(channel_id) -> message_id
)
binds_store = JsonStore(
    "binds.json",
    # binds: str(bind_id) -> {id, owner_id, name, title, body, role_ids, buttons, reactions,
    # created_at, updated_at} — личные шаблоны рассылки для каждого owner/dep.own (cogs/announce.py)
    {"next_id": 1, "binds": {}},
)
