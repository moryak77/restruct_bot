from __future__ import annotations

import copy
import json
from typing import Any

from core.turso_http import execute_blocking, execute_nowait

# Хранилище переехало с локальных JSON-файлов на Turso (libSQL) — у Render'а бесплатный
# план стирает файловую систему контейнера при каждом redeploy/restart, так что локальные
# файлы не переживали обновления бота. Один общий SQL-запрос при старте вытягивает все
# ключи разом (кэш в памяти), поэтому создание ~20 JsonStore ниже не бьёт по Turso по разу
# на каждый стор. Публичный интерфейс JsonStore (load()/save()) не изменился — коги трогать
# не нужно.

_cache: dict[str, Any] = {}
_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    results = execute_blocking(
        [
            ("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)", None),
            ("SELECT key, value FROM kv_store", None),
        ]
    )
    select_result = results[1]["response"]["result"]
    for row in select_result.get("rows", []):
        key, value = row[0]["value"], row[1]["value"]
        _cache[key] = json.loads(value)
    _loaded = True


class JsonStore:
    """Персистентное хранилище на базе одной строки в общей таблице Turso `kv_store`
    (key = имя файла, value = JSON-сериализованные данные).

    Распарсенный JSON держится в памяти: load() отдаёт deepcopy кэша (не ходит в сеть на
    каждый вызов), мутировать результат до save() безопасно. save() остаётся синхронным для
    вызывающего кода (менять сигнатуру во всех ~20 когах не нужно) — запись в Turso уходит
    в фоне через выделенный поток с своим event loop (core/turso_http.py)."""

    def __init__(self, filename: str, default: Any):
        self._key = filename
        self._default = default
        _ensure_loaded()
        if self._key not in _cache:
            _cache[self._key] = copy.deepcopy(default)
            self._persist(_cache[self._key])

    def load(self) -> Any:
        return copy.deepcopy(_cache.get(self._key, self._default))

    def save(self, data: Any) -> None:
        _cache[self._key] = data
        # Замораживаем снимок для фоновой записи — вызывающий код мог бы продолжить
        # мутировать `data` сразу после save(), это не должно повлиять на то, что реально
        # уйдёт в Turso.
        self._persist(copy.deepcopy(data))

    def _persist(self, data: Any) -> None:
        execute_nowait(
            "INSERT INTO kv_store (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [self._key, json.dumps(data, ensure_ascii=False)],
            key=self._key,
        )


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
verify_store = JsonStore(
    "verify.json",
    # codes: str(code) -> {user_id, guild_id, username, display_avatar_url, nickname,
    # guild_joined_at, created_at, expires_at, used} — снимок участника на момент выдачи кода
    # (cogs/verify.py), чтобы сайт мог привязать аккаунт без повторного похода в Discord API.
    {"codes": {}},
)
account_store = JsonStore(
    "account.json",
    # codes: str(code) -> {user_id, purpose, created_at, expires_at} - одноразовые коды
    # подтверждения личности для смены пароля/почты/ника на сайте (cogs/account.py).
    {"codes": {}},
)
coins_store = JsonStore(
    "coins.json",
    # claims: claim_id -> данные заявки на R-Coins с сайта; by_message: id сообщения модерации -> claim_id
    {"counter": 0, "claims": {}, "by_message": {}},
)
