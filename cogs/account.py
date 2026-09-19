from __future__ import annotations

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

# purpose -> (заголовок кода, что делает код на сайте, путь страницы на сайте)
PURPOSES: dict[str, tuple[str, str, str]] = {
    "password": ("Смена пароля", "сменить пароль", "/account"),
    "reset": ("Восстановление пароля", "восстановить пароль", "/auth/forgot-password"),
    "email": ("Смена почты", "сменить почту", "/account"),
    "nickname": ("Смена ника", "сменить ник на сайте", "/account"),
}

# Защита от перебора кодов: сайт — единственный клиент ручки погашения, поэтому лимит
# глобальный. 10 неудачных попыток за минуту — и ручка закрывается ещё на минуту.
_FAIL_WINDOW = 60.0
_FAIL_LIMIT = 10
_recent_failures: list[float] = []
_blocked_until = 0.0


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


async def _is_linked(discord_id: int) -> bool | None:
    """True/False — привязан ли Discord к аккаунту сайта, None — не удалось спросить сайт."""
    if not _site_base_url() or not _api_secret():
        return None
    try:
        async with get_session().get(
            f"{_site_base_url()}/api/discord/linked-ids",
            headers={"X-Api-Key": _api_secret()},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return str(discord_id) in set(data.get("ids", []))
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось проверить привязку Discord к сайту: %s", e)
        return None


async def redeem_code(bot: commands.InteractionBot, code: str, purpose: str) -> tuple[int, dict]:
    """Погашает код (одноразово). Возвращает (http_status, json). Код с неподходящим
    назначением не гасится — им нельзя воспользоваться «не для того»."""
    global _blocked_until
    now_ts = time.monotonic()
    if now_ts < _blocked_until:
        return 429, {"error": "too_many_attempts"}

    data = account_store.load()
    _prune(data)
    entry = data["codes"].get(code)
    if entry is None or entry["purpose"] != purpose:
        _recent_failures[:] = [t for t in _recent_failures if now_ts - t < _FAIL_WINDOW]
        _recent_failures.append(now_ts)
        if len(_recent_failures) >= _FAIL_LIMIT:
            _blocked_until = now_ts + _FAIL_WINDOW
            _recent_failures.clear()
        account_store.save(data)
        return 404, {"error": "invalid_code"}

    del data["codes"][code]
    account_store.save(data)

    user_id = entry["user_id"]
    _, action, _ = PURPOSES[purpose]
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

    return 200, {"discordId": str(user_id)}


def _build_panel_embed() -> disnake.Embed:
    return base_embed(
        f"{icon_tag('key')} Управление аккаунтом сайта",
        (
            "Здесь можно безопасно изменить данные аккаунта на сайте RESTRUCT. "
            "Выбери действие — бот выдаст **одноразовый код**, который нужно ввести на сайте.\n\n"
            f"{icon_tag('lock')} **Сменить пароль** — если помнишь текущий\n"
            f"{icon_tag('key')} **Забыл пароль** — восстановление без входа в аккаунт\n"
            f"{icon_tag('link')} **Сменить почту**\n"
            f"{icon_tag('pencil')} **Сменить ник** на сайте\n\n"
            f"{icon_tag('alert')} Работает только если твой Discord уже привязан к аккаунту сайта. "
            f"Код действует **{CODE_TTL_MINUTES} минут** и одноразовый. Никому его не показывай."
        ),
        panel_key="account",
    )


async def _issue_code(inter: disnake.MessageInteraction, purpose: str) -> None:
    linked = await _is_linked(inter.author.id)
    if linked is False:
        await inter.response.send_message(
            "Твой Discord не привязан к аккаунту сайта. Сначала привяжи его в личном "
            "кабинете на сайте (кнопка «Получить код» в канале верификации).",
            ephemeral=True,
        )
        return
    if linked is None:
        await inter.response.send_message(
            "Не удалось связаться с сайтом. Попробуй чуть позже.", ephemeral=True
        )
        return

    data = account_store.load()
    _prune(data)
    # Один активный код на пользователя и назначение — новый аннулирует старый.
    data["codes"] = {
        c: e
        for c, e in data["codes"].items()
        if not (e["user_id"] == inter.author.id and e["purpose"] == purpose)
    }
    code = _generate_code(data["codes"])
    now = dt.datetime.now(dt.timezone.utc)
    data["codes"][code] = {
        "user_id": inter.author.id,
        "purpose": purpose,
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
    }
    account_store.save(data)

    title, action, path = PURPOSES[purpose]
    site = _site_base_url()
    url = f"{site}{path}" if site else "личный кабинет сайта"
    await inter.response.send_message(
        embed=base_embed(
            f"{icon_tag('key')} {title}",
            (
                f"## `{code}`\n\n"
                f"Открой {url} и введи этот код, чтобы {action}.\n"
                f"{icon_tag('alert')} Код действует **{CODE_TTL_MINUTES} минут**, работает один раз. "
                "Не передавай его никому — сотрудники его не спрашивают."
            ),
        ),
        ephemeral=True,
    )


class AccountPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(
        label="Сменить пароль", style=disnake.ButtonStyle.primary, emoji=icon("lock"),
        custom_id="account_password", row=0,
    )
    async def change_password(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await _issue_code(inter, "password")

    @disnake.ui.button(
        label="Забыл пароль", style=disnake.ButtonStyle.danger, emoji=icon("key"),
        custom_id="account_reset", row=0,
    )
    async def forgot_password(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await _issue_code(inter, "reset")

    @disnake.ui.button(
        label="Сменить почту", style=disnake.ButtonStyle.secondary, emoji=icon("link"),
        custom_id="account_email", row=1,
    )
    async def change_email(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await _issue_code(inter, "email")

    @disnake.ui.button(
        label="Сменить ник", style=disnake.ButtonStyle.secondary, emoji=icon("pencil"),
        custom_id="account_nickname", row=1,
    )
    async def change_nickname(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await _issue_code(inter, "nickname")


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
