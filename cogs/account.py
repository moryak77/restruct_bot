from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import string
import time

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config
from core.http import get_session
from core.icons import icon, icon_tag
from core.storage import account_store

log = logging.getLogger("restruct-bot")

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

CODE_TTL_MINUTES = 10
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OIL")
_CODE_LENGTH = 8

# purpose -> (заголовок кода, что делает код, страница на сайте, шаги на сайте)
PURPOSES: dict[str, tuple[str, str, str, str]] = {
    "password": (
        "Смена пароля",
        "сменить пароль",
        "/profile?tab=security",
        "Открой **Профиль → Безопасность → Пароль**, введи текущий и новый пароль — "
        "на последнем шаге сайт попросит этот код.",
    ),
    "reset": (
        "Восстановление пароля",
        "восстановить пароль",
        "/auth/forgot-password",
        "На странице **«Забыл пароль»** введи свой логин или почту, придумай новый пароль — "
        "на последнем шаге сайт попросит этот код.",
    ),
    "email": (
        "Смена почты",
        "сменить почту",
        "/profile?tab=security",
        "Открой **Профиль → Безопасность → Почта**, введи новый адрес и текущий пароль — "
        "на последнем шаге сайт попросит этот код.",
    ),
    "totp": (
        "Сброс двухфакторной защиты",
        "отключить двухфакторную защиту",
        "/profile?tab=security",
        "Открой **Профиль → Безопасность → Двухфакторная защита**, нажми «Потерян доступ к "
        "приложению» и введи пароль — на последнем шаге сайт попросит этот код.",
    ),
}

# Защита от перебора кодов. Ручку погашения зовёт только сайт, поэтому «источник» — IP
# посетителя, который сайт передаёт в теле запроса: 10 неверных попыток за минуту с одного
# IP — и этот IP получает 429 на минуту. Глобальный потолок (запасной) — на случай атаки
# с множества адресов; он высокий, чтобы обычные пользователи одновременно не мешали друг другу.
_FAIL_WINDOW = 60.0
_FAIL_LIMIT_PER_IP = 10
_FAIL_LIMIT_GLOBAL = 300
_failures_by_ip: dict[str, list[float]] = {}
_blocked_ip_until: dict[str, float] = {}
_global_failures: list[float] = []

# Не чаще одного кода в 10 секунд на пользователя и назначение — защита от спама кнопкой.
_ISSUE_COOLDOWN = 10.0
_last_issue: dict[tuple[int, str], float] = {}

# Список привязанных Discord-аккаунтов кэшируется: раньше КАЖДЫЙ клик по кнопке ходил на сайт
# за полным списком до ответа Discord, а у Discord на ответ всего 3 секунды. Кэш живёт 60с;
# отрицательный ответ перепроверяется, если кэш старше 10с (человек мог привязаться только что).
_LINKED_TTL = 60.0
_LINKED_RECHECK = 10.0
_linked_ids: set[str] = set()
_linked_fetched_at = 0.0
_linked_lock = asyncio.Lock()


def _site_base_url() -> str:
    return (config.get("verification.site_base_url") or "").rstrip("/")


def _api_secret() -> str:
    return config.get("verification.api_secret") or ""


def _generate_code(existing: dict) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if code not in existing:
            return code


def _prune(data: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    data["codes"] = {
        code: entry
        for code, entry in data["codes"].items()
        if dt.datetime.fromisoformat(entry["expires_at"]) > now
    }


async def _fetch_linked_ids() -> bool:
    global _linked_ids, _linked_fetched_at
    try:
        async with get_session().get(
            f"{_site_base_url()}/api/discord/linked-ids",
            headers={"X-Api-Key": _api_secret()},
            timeout=8,
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось получить список привязанных аккаунтов: %s", e)
        return False
    _linked_ids = set(data.get("ids", []))
    _linked_fetched_at = time.monotonic()
    return True


async def _is_linked(discord_id: int) -> bool | None:
    """True/False — привязан ли Discord к аккаунту сайта, None — сайт недоступен."""
    if not _site_base_url() or not _api_secret():
        return None
    key = str(discord_id)
    age = time.monotonic() - _linked_fetched_at
    if key in _linked_ids and age < _LINKED_TTL:
        return True
    if age >= _LINKED_RECHECK or _linked_fetched_at == 0.0:
        async with _linked_lock:  # одновременные клики делят один запрос к сайту
            age = time.monotonic() - _linked_fetched_at
            if age >= _LINKED_RECHECK or _linked_fetched_at == 0.0:
                if not await _fetch_linked_ids() and _linked_fetched_at == 0.0:
                    return None
    return key in _linked_ids


def _register_failure(ip: str, now: float) -> None:
    recent = [t for t in _failures_by_ip.get(ip, []) if now - t < _FAIL_WINDOW]
    recent.append(now)
    _failures_by_ip[ip] = recent
    if len(recent) >= _FAIL_LIMIT_PER_IP:
        _blocked_ip_until[ip] = now + _FAIL_WINDOW
        _failures_by_ip.pop(ip, None)

    _global_failures[:] = [t for t in _global_failures if now - t < _FAIL_WINDOW]
    _global_failures.append(now)

    if len(_failures_by_ip) > 5000:  # не даём словарям расти бесконечно
        for k in [k for k, v in _failures_by_ip.items() if not v or now - v[-1] > _FAIL_WINDOW]:
            _failures_by_ip.pop(k, None)
        for k in [k for k, t in _blocked_ip_until.items() if t < now]:
            _blocked_ip_until.pop(k, None)


async def _notify_used(bot: commands.InteractionBot, user_id: int, action: str) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(
            embed=base_embed(
                f"{icon_tag('alert')} Код использован",
                f"Твой код только что использован на сайте, чтобы **{action}**.\n\n"
                "Если это был не ты — срочно смени пароль и напиши администрации.",
            )
        )
    except (disnake.HTTPException, AttributeError):
        pass


async def redeem_code(
    bot: commands.InteractionBot,
    code: str,
    purpose: str,
    client_ip: str = "unknown",
    expected_discord_id: int | None = None,
) -> tuple[int, dict]:
    """Погашает код (одноразово). Возвращает (http_status, json). Код с неподходящим
    назначением не гасится — им нельзя воспользоваться «не для того». Между чтением и
    сохранением нет await, поэтому два одновременных запроса с одним кодом не пройдут оба."""
    now = time.monotonic()
    if _blocked_ip_until.get(client_ip, 0.0) > now or (
        len([t for t in _global_failures if now - t < _FAIL_WINDOW]) >= _FAIL_LIMIT_GLOBAL
    ):
        return 429, {"error": "too_many_attempts"}

    data = account_store.load()
    _prune(data)
    entry = data["codes"].get(code)
    if entry is None or entry["purpose"] != purpose:
        _register_failure(client_ip, now)
        account_store.save(data)
        return 404, {"error": "invalid_code"}

    # Код принадлежит конкретному Discord-аккаунту: если сайт ждёт код другого человека
    # (например, код одного игрока ввели в аккаунте другого) — отказываем и НЕ гасим код,
    # чтобы им нельзя было «сжечь» чужой код или воспользоваться им не тем аккаунтом.
    if expected_discord_id is not None and entry["user_id"] != expected_discord_id:
        _register_failure(client_ip, now)
        account_store.save(data)
        return 403, {"error": "wrong_owner"}

    del data["codes"][code]
    account_store.save(data)

    _, action, _, _ = PURPOSES[purpose]
    # ЛС владельцу — в фоне: сайт не должен ждать ответа Discord API, чтобы получить ответ.
    asyncio.create_task(_notify_used(bot, entry["user_id"], action))
    return 200, {"discordId": str(entry["user_id"])}


def _build_panel_embed() -> disnake.Embed:
    site = _site_base_url()
    site_line = f"[{site.replace('https://', '')}]({site})" if site else "сайте семьи"
    return base_embed(
        f"{icon_tag('key')} Управление аккаунтом сайта",
        (
            f"Здесь ты можешь безопасно управлять аккаунтом на {site_line}. "
            "Выбери действие в списке под сообщением — бот выдаст **одноразовый код**, "
            "а остальное делается на сайте.\n\n"
            "**Что доступно**\n"
            f"{icon_tag('lock')} **Смена пароля** — если помнишь текущий пароль\n"
            f"{icon_tag('key')} **Забыл пароль** — восстановление без входа в аккаунт\n"
            f"{icon_tag('link')} **Смена почты** — привязка нового адреса\n"
            f"{icon_tag('lock')} **Двухфакторная защита** — если потерян доступ к приложению-аутентификатору\n\n"
            "**Как это работает**\n"
            "**1.** Выбери действие в списке — код придёт только тебе (скрытое сообщение).\n"
            "**2.** Открой сайт и заполни нужные данные: текущий пароль, новое значение и т.д.\n"
            "**3.** На последнем шаге введи код — и изменение применится.\n\n"
            "**Безопасность**\n"
            f"{icon_tag('lock')} Код создаётся **для твоего Discord-аккаунта** и подходит только к "
            "аккаунту сайта, привязанному к нему. Чужой код не сработает, а твой не сработает у другого.\n"
            f"{icon_tag('alert')} Код действует **{CODE_TTL_MINUTES} минут** и работает один раз. "
            "Никому его не показывай — сотрудники его не спрашивают.\n"
            f"{icon_tag('pencil')} Логин на сайте меняется прямо в профиле — код для этого не нужен.\n\n"
            f"{icon_tag('alert')} Работает только если твой Discord уже привязан к аккаунту сайта "
            "(канал верификации)."
        ),
        panel_key="account",
    )


async def _issue_code(inter: disnake.MessageInteraction, purpose: str) -> None:
    # Discord даёт на ответ 3 секунды — подтверждаем взаимодействие сразу, дальше работаем
    # сколько нужно и отвечаем followup'ом (ephemeral — код видит только нажавший).
    await inter.response.defer(ephemeral=True)

    now = time.monotonic()
    cooldown_key = (inter.author.id, purpose)
    wait = _ISSUE_COOLDOWN - (now - _last_issue.get(cooldown_key, -_ISSUE_COOLDOWN))
    if wait > 0:
        await inter.edit_original_response(
            content=f"Подожди {int(wait) + 1} сек. перед повторным запросом кода."
        )
        return

    linked = await _is_linked(inter.author.id)
    if linked is False:
        await inter.edit_original_response(
            content=(
                "Твой Discord не привязан к аккаунту сайта. Сначала привяжи его в личном "
                "кабинете на сайте (кнопка «Получить код» в канале верификации)."
            )
        )
        return
    if linked is None:
        await inter.edit_original_response(content="Не удалось связаться с сайтом. Попробуй чуть позже.")
        return

    _last_issue[cooldown_key] = now
    if len(_last_issue) > 5000:
        for k in [k for k, t in _last_issue.items() if now - t > _ISSUE_COOLDOWN]:
            _last_issue.pop(k, None)

    data = account_store.load()
    _prune(data)
    # Один активный код на пользователя и назначение — новый аннулирует старый.
    data["codes"] = {
        c: e
        for c, e in data["codes"].items()
        if not (e["user_id"] == inter.author.id and e["purpose"] == purpose)
    }
    code = _generate_code(data["codes"])
    created = dt.datetime.now(dt.timezone.utc)
    data["codes"][code] = {
        "user_id": inter.author.id,
        "purpose": purpose,
        "created_at": created.isoformat(),
        "expires_at": (created + dt.timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
    }
    account_store.save(data)

    title, action, path, steps = PURPOSES[purpose]
    site = _site_base_url()
    url = f"{site}{path}" if site else "личный кабинет сайта"
    await inter.edit_original_response(
        content=None,
        embed=base_embed(
            f"{icon_tag('key')} {title}",
            (
                f"## `{code}`\n\n"
                f"**Что делать дальше**\n{steps}\n\n"
                f"Ссылка: {url}\n\n"
                f"{icon_tag('lock')} Код выдан **твоему Discord-аккаунту** ({inter.author.mention}) и подходит "
                f"только для аккаунта сайта, привязанного к нему — чтобы {action}.\n"
                f"{icon_tag('alert')} Действует **{CODE_TTL_MINUTES} минут**, работает один раз. "
                "Не передавай его никому."
            ),
        ),
    )


_ACTION_OPTIONS = [
    ("password", "Сменить пароль", "Знаю текущий пароль и хочу поставить новый", "lock"),
    ("reset", "Забыл пароль", "Восстановить доступ без входа в аккаунт", "key"),
    ("email", "Сменить почту", "Привязать другой адрес электронной почты", "link"),
    ("totp", "Сбросить двухфакторную защиту", "Потерян доступ к приложению-аутентификатору", "lock"),
]


class AccountPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        site = _site_base_url()
        if site:
            self.add_item(
                disnake.ui.Button(label="Открыть сайт", url=f"{site}/profile?tab=security", emoji=icon("link"))
            )

    @disnake.ui.string_select(
        custom_id="account_action",
        placeholder="Выбери действие…",
        min_values=1,
        max_values=1,
        options=[
            disnake.SelectOption(label=label, value=value, description=desc, emoji=icon(icon_key))
            for value, label, desc, icon_key in _ACTION_OPTIONS
        ],
    )
    async def choose_action(self, select: disnake.ui.StringSelect, inter: disnake.MessageInteraction):
        purpose = select.values[0]
        if purpose not in PURPOSES:
            await inter.response.send_message("Неизвестное действие.", ephemeral=True)
            return
        await _issue_code(inter, purpose)
        # Сбрасываем выбранный пункт в списке — панелью пользуются все по очереди.
        try:
            await inter.message.edit(view=AccountPanelView())
        except disnake.HTTPException:
            pass


class Account(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="account",
        description="Управление аккаунтом сайта",
        default_member_permissions=ADMIN_PERMS,
    )
    async def account(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @account.sub_command(name="show", description="Опубликовать панель управления аккаунтом в этом канале")
    async def account_show(self, inter: disnake.ApplicationCommandInteraction):
        await send_panel(inter, _build_panel_embed(), view=AccountPanelView(), panel_key="account")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Account(bot))


PERSISTENT_VIEWS = [lambda: AccountPanelView()]
