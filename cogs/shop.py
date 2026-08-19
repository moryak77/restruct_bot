from __future__ import annotations

import asyncio
import datetime as dt
import secrets

import disnake
from disnake.ext import commands, tasks

from cogs.tickets import (
    AdminReplyModal,
    _build_close_log_embed,
    _build_transcript,
    _create_ticket_channel,
    _staff_role_ids,
)
from core.branding import base_embed, send_panel
from core.checks import is_staff
from core.config import config, log_channel_id
from core.icons import icon, icon_tag
from core.storage import payments_store, shop_store, tickets_store
from core.yookassa import configured as yookassa_configured
from core.yookassa import create_payment as yookassa_create_payment
from core.yookassa import get_payment as yookassa_get_payment
from core.yoomoney import build_payment_link, configured as yoomoney_configured, fetch_recent_operations

POLL_SECONDS = config.get("yoomoney.poll_interval_seconds", 25) or 25
ORDER_EXPIRY_MINUTES = config.get("yoomoney.order_expiry_minutes", 60) or 60
WEBHOOK_RELAY_CHANNEL_ID = config.get("yookassa.webhook_relay_channel_id")


def payments_configured() -> bool:
    return yookassa_configured() or yoomoney_configured()

# Свой набор кастомных иконок (Tabler Icons, тот же серый тон #B5BAC1, что у остального бота).
_SHOP_ICON_IDS = {
    "cart": 1538632113321025627,
    "tag": 1538632119469871194,
    "design": 1538632124935045202,
    "editor": 1538632131062800505,
    "reklama": 1538632137027358862,
    "bots": 1538632142903574548,
}
SHOP_EMOJI = {key: disnake.PartialEmoji(name=f"shop_{key}", id=emoji_id) for key, emoji_id in _SHOP_ICON_IDS.items()}


def _services() -> list[dict]:
    return shop_store.load()["services"]


def _find_service(key: str) -> dict | None:
    return next((s for s in _services() if s["key"] == key), None)


def _service_icon(service: dict) -> disnake.PartialEmoji:
    return SHOP_EMOJI.get(service.get("icon"), SHOP_EMOJI["cart"])


def _fmt_price(service: dict) -> str:
    price = service.get("price")
    if not price:
        return "по сложности"
    return f"{price:,.0f} ₽".replace(",", " ")


def _fmt_amount(amount: float) -> str:
    return f"{amount:,.0f} ₽".replace(",", " ")


def _build_shop_embed() -> disnake.Embed:
    services = _services()
    has_fixed = any(s.get("price") for s in services)
    has_quote = any(not s.get("price") for s in services)

    embed = base_embed(
        f"{SHOP_EMOJI['cart']} Магазин услуг семьи RESTRUCT",
        "Нажмите на кнопку нужной услуги, чтобы оформить заказ. Обработкой занимается "
        "выделенная команда — обычные модераторы к магазину доступа не имеют.",
        panel_key="shop",
    )

    if has_quote and not has_fixed:
        # Сейчас у всех услуг цена «по сложности» — фиксированной цены нет ни у одной,
        # поэтому показываем только этот сценарий, без упоминания прямой оплаты по ссылке.
        how_it_works = (
            "**1.** Нажмите кнопку услуги, которая вам нужна.\n"
            "**2.** В открывшемся окне опишите задание — что нужно сделать, пожелания, сроки.\n"
            "**3.** Бот откроет для вас приватный тикет-канал. Там персонал оценит сложность "
            "и пришлёт ссылку на оплату.\n"
            "**4.** Оплатите по ссылке любым удобным способом — картой, через СБП или с "
            "электронного кошелька. Бот сам проверит оплату и подтвердит её прямо в тикете."
        )
    elif has_fixed and has_quote:
        how_it_works = (
            "**1.** Нажмите кнопку услуги, которая вам нужна.\n"
            f"{icon_tag('coin')} **Если у услуги указана цена** — перейдите по ссылке и "
            "оплатите любым удобным способом (картой, через СБП или с электронного "
            "кошелька). Бот сам проверит оплату и откроет тикет-канал, куда пришлёт "
            "ссылку в ЛС.\n"
            f"{icon_tag('coin')} **Если у услуги цена «по сложности»** — в открывшемся окне "
            "опишите задание, бот сразу откроет тикет. Персонал оценит сложность и "
            "пришлёт ссылку на оплату прямо туда.\n"
            "В любом случае детали заказа вы обсудите с командой в открывшемся тикете."
        )
    else:
        how_it_works = (
            "**1.** Нажмите кнопку услуги, которая вам нужна.\n"
            "**2.** Перейдите по ссылке и оплатите любым удобным способом — картой, через СБП "
            "или с электронного кошелька.\n"
            "**3.** Бот сам проверит оплату и откроет тикет-канал, куда пришлёт ссылку в ЛС.\n"
            "**4.** В канале вы обсудите детали заказа с командой."
        )

    embed.add_field(name=f"{icon_tag('help')} Как это работает", value=how_it_works, inline=False)

    if not payments_configured():
        embed.add_field(
            name="⚠️ Внимание",
            value="Приём оплаты сейчас не настроен — обратитесь к руководству.",
            inline=False,
        )

    if not services:
        embed.add_field(name=f"{SHOP_EMOJI['tag']} Услуги", value="Пока ничего не добавлено.", inline=False)
        return embed

    lines = []
    for service in services:
        lines.append(f"{_service_icon(service)} **{service['label']}** — {_fmt_price(service)}\n┗ {service.get('description') or '—'}")
    embed.add_field(name=f"{SHOP_EMOJI['tag']} Услуги", value="\n\n".join(lines)[:4000], inline=False)
    return embed


async def _refresh_shop_panel(guild: disnake.Guild) -> None:
    data = shop_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    embed = _build_shop_embed()
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=ShopPanelView())
    except disnake.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Заказ услуги — создание платежа
# ---------------------------------------------------------------------------

def _create_order(user_id: int, guild_id: int, service: dict) -> dict:
    data = payments_store.load()
    order_id = data["next_id"]
    data["next_id"] += 1
    order = {
        "id": order_id,
        "label": f"shop-{order_id}-{secrets.token_hex(4)}",
        "yookassa_payment_id": None,
        "user_id": user_id,
        "guild_id": guild_id,
        "service_key": service["key"],
        "service_label": service["label"],
        "amount": float(service["price"]),
        "status": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "paid_at": None,
        "ticket_channel_id": None,
    }
    data["orders"].append(order)
    payments_store.save(data)
    return order


def _create_quote_order(user_id: int, guild_id: int, service: dict, channel_id: int, task_description: str) -> dict:
    """Заказ «по сложности» — цена ещё не известна, тикет уже открыт (channel_id указывает
    на него сразу, в отличие от обычного заказа, где ticket_channel_id появляется только
    после оплаты). Статус awaiting_price не трогается фоновым опросом до тех пор, пока
    персонал не назначит цену — тогда заказ переходит в обычный pending."""
    data = payments_store.load()
    order_id = data["next_id"]
    data["next_id"] += 1
    order = {
        "id": order_id,
        "label": f"shop-{order_id}-{secrets.token_hex(4)}",
        "yookassa_payment_id": None,
        "user_id": user_id,
        "guild_id": guild_id,
        "service_key": service["key"],
        "service_label": service["label"],
        "amount": None,
        "status": "awaiting_price",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "paid_at": None,
        "ticket_channel_id": channel_id,
        "task_description": task_description,
    }
    data["orders"].append(order)
    payments_store.save(data)
    return order


async def _issue_payment_link(order: dict, description: str) -> str | None:
    """Создаёт платёж у настроенного провайдера (приоритет — ЮKassa) и возвращает ссылку
    на оплату, либо None при ошибке. Для ЮKassa попутно сохраняет payment_id в заказе —
    он нужен фоновому опросу для сверки статуса."""
    if yookassa_configured():
        payment = await yookassa_create_payment(order["amount"], description, order["id"], idempotence_key=order["label"])
        if payment is None or "confirmation" not in payment:
            return None
        data = payments_store.load()
        for stored in data["orders"]:
            if stored["id"] == order["id"]:
                stored["yookassa_payment_id"] = payment["id"]
                break
        payments_store.save(data)
        return payment["confirmation"]["confirmation_url"]
    return build_payment_link(order["amount"], order["label"], description)


def _payment_note() -> str:
    if yookassa_configured():
        return "Оплата пройдёт через ЮKassa — как только платёж подтвердится, тикет откроется практически мгновенно."
    return "Как только оплата поступит, бот сам всё проверит (обычно в течение полуминуты) и откроет тикет."


async def _start_payment(inter: disnake.MessageInteraction, service: dict) -> None:
    if not service.get("price"):
        await inter.response.send_message("❌ Цена на эту услугу — «по сложности», нажмите кнопку ещё раз.", ephemeral=True)
        return
    if not payments_configured():
        await inter.response.send_message("❌ Приём оплаты сейчас не настроен — обратитесь к руководству.", ephemeral=True)
        return

    await inter.response.defer(ephemeral=True)
    order = _create_order(inter.author.id, inter.guild_id, service)
    description = f"{service['label']} — заказ №{order['id']}"

    link = await _issue_payment_link(order, description)
    if link is None:
        await inter.followup.send(
            "❌ Не удалось создать платёж — попробуйте ещё раз чуть позже или обратитесь к руководству.",
            ephemeral=True,
        )
        return
    note = _payment_note()

    embed = base_embed(
        f"{_service_icon(service)} Оплата: {service['label']}",
        f"Сумма к оплате: **{_fmt_price(service)}**\n\n"
        "Перейдите по кнопке ниже и оплатите любым удобным способом — картой, через СБП "
        f"или с электронного кошелька. {note} Ссылку на тикет пришлём в личные сообщения.",
    )
    embed.add_field(name="Номер заказа", value=f"№{order['id']}", inline=True)
    embed.add_field(name="Сумма", value=_fmt_price(service), inline=True)
    embed.set_footer(text="Ссылка одноразовая и привязана к этому заказу — не передавайте её другим.")

    view = disnake.ui.View()
    view.add_item(
        disnake.ui.Button(label=f"Оплатить {_fmt_price(service)}", style=disnake.ButtonStyle.link, url=link, emoji="💳")
    )

    await inter.followup.send(embed=embed, view=view, ephemeral=True)


class ShopPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for service in _services()[:25]:
            self.add_item(self._make_button(service))

    @staticmethod
    def _make_button(service: dict) -> disnake.ui.Button:
        button: disnake.ui.Button = disnake.ui.Button(
            label=service["label"][:80],
            emoji=_service_icon(service),
            style=disnake.ButtonStyle.secondary,
            custom_id=f"shop_order_{service['key']}",
        )

        async def callback(inter: disnake.MessageInteraction, service=service):
            fresh = _find_service(service["key"])
            if fresh is None:
                await inter.response.send_message("❌ Эта услуга больше не найдена в конфиге.", ephemeral=True)
                return
            if not fresh.get("price"):
                if not payments_configured():
                    await inter.response.send_message(
                        "❌ Приём оплаты сейчас не настроен — обратитесь к руководству.", ephemeral=True
                    )
                    return
                await inter.response.send_modal(QuoteRequestModal(fresh))
                return
            await _start_payment(inter, fresh)

        button.callback = callback
        return button


class QuoteRequestModal(disnake.ui.Modal):
    """Открывается вместо оплаты для услуг с ценой «по сложности» — покупатель описывает
    задание, бот сразу создаёт тикет (как у /ticket). Цену персонал назначает не в самом
    тикете, а в отдельном канале модерации — см. QuoteListView/_refresh_quote_list."""

    def __init__(self, service: dict):
        self.service = service
        components = [
            disnake.ui.TextInput(
                label="Опишите задание",
                custom_id="description",
                style=disnake.TextInputStyle.paragraph,
                max_length=1000,
                placeholder="Что нужно сделать, пожелания, сроки и т.д.",
            ),
        ]
        super().__init__(title=f"Заказ: {service['label']}"[:45], components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        description = inter.text_values["description"].strip()

        channel = await _create_ticket_channel(
            inter,
            "shop",
            None,
            category_label=self.service["label"],
            extra_fields=[
                ("Услуга", self.service["label"]),
                ("Задание", description),
            ],
            name_suffix=f"{self.service['key']}-quote",
            embed_title=f"{_service_icon(self.service)} Заказ по сложности: {self.service['label']}",
            external_moderation=True,
            status_note="Оценка сложности и стоимости — персонал уже видит эту заявку в канале модерации заказов.",
        )
        if channel is None:
            return

        _create_quote_order(inter.author.id, inter.guild_id, self.service, channel.id, description)
        await _refresh_quote_list(inter.guild)

        await inter.followup.send(f"✅ Заявка создана: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Администрирование: добавление / изменение / удаление услуг
# ---------------------------------------------------------------------------

class ServiceModal(disnake.ui.Modal):
    def __init__(self, existing: dict | None = None):
        self.existing_key = existing["key"] if existing else None
        components = [
            disnake.ui.TextInput(
                label="Название услуги",
                custom_id="label",
                style=disnake.TextInputStyle.short,
                max_length=80,
                value=existing["label"] if existing else None,
            ),
            disnake.ui.TextInput(
                label="Цена в рублях (пусто = по сложности)",
                custom_id="price",
                style=disnake.TextInputStyle.short,
                max_length=10,
                placeholder="Например: 500, или оставьте пустым",
                value=str(int(existing["price"])) if existing and existing.get("price") else None,
                required=False,
            ),
            disnake.ui.TextInput(
                label="Описание",
                custom_id="description",
                style=disnake.TextInputStyle.paragraph,
                max_length=300,
                value=existing.get("description") if existing else None,
                required=False,
            ),
        ]
        super().__init__(title="Изменить услугу" if existing else "Добавить услугу", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        label = inter.text_values["label"].strip()
        price_raw = inter.text_values["price"].strip()
        description = inter.text_values["description"].strip()
        if not label:
            await inter.response.send_message("❌ Укажите название услуги.", ephemeral=True)
            return

        price = None
        if price_raw:
            try:
                price = float(price_raw.replace(",", "."))
            except ValueError:
                price = None
            if price is None or price <= 0:
                await inter.response.send_message("❌ Цена должна быть положительным числом (например: 500).", ephemeral=True)
                return

        data = shop_store.load()
        if self.existing_key:
            service = next((s for s in data["services"] if s["key"] == self.existing_key), None)
            if service is None:
                await inter.response.send_message("❌ Эта услуга больше не найдена — возможно, её уже удалили.", ephemeral=True)
                return
            service["label"] = label
            service["price"] = price
            service["description"] = description
        else:
            key = label.lower().replace(" ", "_")[:40]
            if any(s["key"] == key for s in data["services"]):
                key = f"{key}_{len(data['services']) + 1}"
            data["services"].append(
                {"key": key, "label": label, "icon": "cart", "price": price, "description": description}
            )
        shop_store.save(data)

        await inter.response.send_message(f"✅ Услуга **{label}** сохранена.", ephemeral=True)
        await _refresh_shop_panel(inter.guild)


class RemoveServiceSelect(disnake.ui.StringSelect):
    def __init__(self, services: list[dict]):
        options = [
            disnake.SelectOption(label=s["label"][:100], value=s["key"], description=_fmt_price(s)[:100])
            for s in services[:25]
        ]
        super().__init__(placeholder="Выберите услугу для удаления", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        data = shop_store.load()
        before = len(data["services"])
        removed = next((s for s in data["services"] if s["key"] == self.values[0]), None)
        data["services"] = [s for s in data["services"] if s["key"] != self.values[0]]
        shop_store.save(data)

        if removed and len(data["services"]) < before:
            await inter.response.edit_message(content=f"🗑️ Услуга **{removed['label']}** удалена.", view=None)
            await _refresh_shop_panel(inter.guild)
        else:
            await inter.response.edit_message(content="❌ Услуга не найдена, возможно уже удалена.", view=None)


class EditServiceSelect(disnake.ui.StringSelect):
    def __init__(self, services: list[dict]):
        options = [
            disnake.SelectOption(label=s["label"][:100], value=s["key"], description=_fmt_price(s)[:100])
            for s in services[:25]
        ]
        super().__init__(placeholder="Выберите услугу для изменения", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        service = _find_service(self.values[0])
        if service is None:
            await inter.response.edit_message(content="❌ Услуга не найдена, возможно уже удалена.", view=None)
            return
        await inter.response.send_modal(ServiceModal(service))


class RemoveServiceView(disnake.ui.View):
    def __init__(self, services: list[dict]):
        super().__init__(timeout=120)
        self.add_item(RemoveServiceSelect(services))


class EditServiceView(disnake.ui.View):
    def __init__(self, services: list[dict]):
        super().__init__(timeout=120)
        self.add_item(EditServiceSelect(services))


class ShopAdminView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Добавить услугу", style=disnake.ButtonStyle.secondary, emoji="➕", custom_id="shop_add")
    async def add_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(ServiceModal())

    @disnake.ui.button(label="Изменить услугу", style=disnake.ButtonStyle.secondary, emoji="✏️", custom_id="shop_edit")
    async def edit_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        services = _services()
        if not services:
            await inter.response.send_message("Услуг пока нет — сначала добавьте хотя бы одну.", ephemeral=True)
            return
        await inter.response.send_message("Выберите услугу, которую нужно изменить:", view=EditServiceView(services), ephemeral=True)

    @disnake.ui.button(label="Удалить услугу", style=disnake.ButtonStyle.secondary, emoji="🗑️", custom_id="shop_remove")
    async def remove_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        services = _services()
        if not services:
            await inter.response.send_message("Услуг пока нет — удалять нечего.", ephemeral=True)
            return
        await inter.response.send_message("Выберите услугу, которую нужно удалить:", view=RemoveServiceView(services), ephemeral=True)


# ---------------------------------------------------------------------------
# Проверка оплаты и открытие тикета
# ---------------------------------------------------------------------------

class _FollowupStub:
    """_create_ticket_channel зовёт inter.followup.send только если не хватило прав
    создать канал — в фоновой задаче реальной interaction нет, репортим отдельно."""

    async def send(self, *args, **kwargs) -> None:
        pass


class _InteractionStub:
    """Минимальная замена disnake.Interaction для вызова _create_ticket_channel из
    фоновой задачи (после подтверждения оплаты), где никакой реальной interaction нет."""

    def __init__(self, guild: disnake.Guild, author: disnake.Member):
        self.guild = guild
        self.author = author
        self.followup = _FollowupStub()


QUOTE_STATUS_LABEL = {
    "awaiting_price": ("pending", "ожидает цену"),
    "pending": ("coin", "ожидает оплату"),
    "paid": ("check", "оплачено"),
}


class QuotePriceModal(disnake.ui.Modal):
    """Открывается из рабочей панели в канале модерации (QuoteWorkView), а не из самого
    тикета — поэтому ссылку на оплату отправляем не в inter.channel, а явно в
    order['ticket_channel_id']."""

    def __init__(self, order_id: int):
        self.order_id = order_id
        components = [
            disnake.ui.TextInput(
                label="Цена в рублях (только число)",
                custom_id="price",
                style=disnake.TextInputStyle.short,
                max_length=10,
                placeholder="Например: 2500",
            ),
        ]
        super().__init__(title="Установить цену заказа", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["price"].strip()
        try:
            price = float(raw.replace(",", "."))
        except ValueError:
            price = None
        if price is None or price <= 0:
            await inter.response.send_message("❌ Цена должна быть положительным числом (например: 2500).", ephemeral=True)
            return
        if not payments_configured():
            await inter.response.send_message("❌ Приём оплаты сейчас не настроен — обратитесь к руководству.", ephemeral=True)
            return

        data = payments_store.load()
        order = next((o for o in data["orders"] if o["id"] == self.order_id), None)
        if order is None or order["status"] != "awaiting_price":
            await inter.response.send_message("❌ Этот заказ уже обработан.", ephemeral=True)
            return

        ticket_channel = inter.guild.get_channel(order["ticket_channel_id"])
        if ticket_channel is None:
            await inter.response.send_message("❌ Тикет этого заказа был удалён вручную.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        order["amount"] = price
        order["status"] = "pending"
        order["priced_by"] = inter.author.id
        payments_store.save(data)

        description = f"{order['service_label']} — заказ №{order['id']}"
        link = await _issue_payment_link(order, description)
        if link is None:
            data = payments_store.load()
            for stored in data["orders"]:
                if stored["id"] == order["id"]:
                    stored["amount"] = None
                    stored["status"] = "awaiting_price"
                    break
            payments_store.save(data)
            await inter.followup.send("❌ Не удалось создать платёж — попробуйте ещё раз чуть позже.", ephemeral=True)
            return

        price_text = _fmt_amount(price)
        pay_embed = base_embed(
            f"{icon_tag('coin')} Цена установлена: {price_text}",
            f"Оплатите по кнопке ниже любым удобным способом — картой, через СБП или с "
            f"электронного кошелька. {_payment_note()}",
        )
        pay_embed.set_footer(text="Ссылка одноразовая и привязана к этому заказу — не передавайте её другим.")
        pay_view = disnake.ui.View()
        pay_view.add_item(
            disnake.ui.Button(label=f"Оплатить {price_text}", style=disnake.ButtonStyle.link, url=link, emoji="💳")
        )
        await ticket_channel.send(content=f"<@{order['user_id']}>", embed=pay_embed, view=pay_view)

        await _refresh_quote_list(inter.guild)
        await inter.followup.send(
            f"✅ Цена установлена, ссылка на оплату отправлена в {ticket_channel.mention}.", ephemeral=True
        )


def _quote_orders_unchecked() -> list[dict]:
    """Как _quote_orders, но без проверки, что канал тикета ещё существует — используется
    только при регистрации persistent-view на старте бота, когда кэш гильдий может быть ещё
    не готов. Актуальный список с очисткой протухших записей строит _refresh_quote_list."""
    data = payments_store.load()
    orders = [o for o in data["orders"] if o.get("task_description") is not None and o["status"] in QUOTE_STATUS_LABEL]
    orders.sort(key=lambda o: o["id"])
    return orders


def _quote_orders(guild: disnake.Guild) -> list[dict]:
    data = payments_store.load()
    orders = [
        o
        for o in data["orders"]
        if o.get("task_description") is not None
        and o["status"] in QUOTE_STATUS_LABEL
        and o.get("ticket_channel_id")
        and guild.get_channel(o["ticket_channel_id"]) is not None
    ]
    orders.sort(key=lambda o: o["id"])
    return orders


def _build_quote_list_embed(orders: list[dict]) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('coin')} Заказы «по сложности»",
        "Центр модерации заказов с ценой по договорённости. Выберите заказ в меню под этим "
        "сообщением, чтобы назначить цену — список обновляется автоматически.",
        panel_key="shop_moderation",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            "**1.** Выберите заказ в меню ниже — откроется рабочая панель, видимая только вам.\n"
            f"**2.** Нажмите {icon_tag('coin')} **Установить цену** и укажите сумму.\n"
            "**3.** Бот сам отправит ссылку на оплату в тикет покупателя и подтвердит оплату "
            "прямо там, когда она пройдёт."
        ),
        inline=False,
    )

    if not orders:
        embed.add_field(name=f"{icon_tag('coin')} Заказы", value="Сейчас нет активных заказов «по сложности».", inline=False)
        return embed

    lines = []
    for o in orders:
        icon_key, label = QUOTE_STATUS_LABEL.get(o["status"], ("unassigned", o["status"]))
        amount = _fmt_amount(o["amount"]) if o.get("amount") else "—"
        lines.append(
            f"`№{o['id']}` **{o['service_label']}**\n"
            f"┗ <#{o['ticket_channel_id']}> · <@{o['user_id']}> · {icon_tag(icon_key)} {label} · {amount}"
        )
    value = "\n\n".join(lines)[:4000]
    embed.add_field(name=f"{icon_tag('coin')} Заказы ({len(orders)})", value=value, inline=False)
    return embed


def _quote_ticket_info(order: dict) -> dict | None:
    tdata = tickets_store.load()
    return tdata["open"].get(str(order["ticket_channel_id"]))


def _build_quote_work_embed(order: dict, ticket_info: dict | None) -> disnake.Embed:
    embed = base_embed(f"{icon_tag('coin')} Заказ №{order['id']} — {order['service_label']}", panel_key="shop_moderation")
    embed.add_field(name="Покупатель", value=f"<@{order['user_id']}>", inline=True)
    embed.add_field(name="Тикет", value=f"<#{order['ticket_channel_id']}>", inline=True)
    claimed_by = ticket_info.get("claimed_by") if ticket_info else None
    embed.add_field(
        name="Ответственный",
        value=f"<@{claimed_by}>" if claimed_by else f"{icon_tag('unassigned')} не назначен",
        inline=True,
    )
    icon_key, label = QUOTE_STATUS_LABEL.get(order["status"], ("unassigned", order["status"]))
    embed.add_field(name="Статус оплаты", value=f"{icon_tag(icon_key)} {label}", inline=True)
    if order.get("amount"):
        embed.add_field(name="Цена", value=_fmt_amount(order["amount"]), inline=True)
    if order.get("priced_by"):
        embed.add_field(name="Цену назначил", value=f"<@{order['priced_by']}>", inline=True)
    embed.add_field(name="Задание", value=order.get("task_description") or "—", inline=False)

    claim_hint = "" if claimed_by else " _(сначала заберите заказ)_"
    embed.add_field(
        name=f"{icon_tag('settings')} Кнопки",
        value=(
            f"{icon_tag('hand')} — **Забрать** заказ в обработку\n"
            f"{icon_tag('shield')} — Ответить **от администрации** в тикете (анонимно)\n"
            f"{icon_tag('coin')} — **Установить цену**{claim_hint}\n"
            f"{icon_tag('lock')} — **Закрыть** тикет"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Заказ №{order['id']} • Семья RESTRUCT")
    return embed


class QuoteWorkView(disnake.ui.View):
    """Рабочая эфемерная панель для одного заказа — открывается через выбор в списке
    QuoteListView. По той же схеме, что TicketWorkView у заявок в семью (см. cogs/tickets.py):
    статус клейма живёт в tickets_store (общий с обычными тикетами), цена/оплата — в
    payments_store."""

    def __init__(self, order: dict, ticket_info: dict | None):
        super().__init__(timeout=300)
        self.order_id = order["id"]
        self.channel_id = order["ticket_channel_id"]

        claimed = bool(ticket_info and ticket_info.get("claimed_by"))
        if claimed:
            self.claim_button.disabled = True
        if order["status"] != "awaiting_price" or not claimed:
            self.set_price_button.disabled = True

    def _load(self) -> tuple[dict | None, dict | None]:
        pdata = payments_store.load()
        order = next((o for o in pdata["orders"] if o["id"] == self.order_id), None)
        tdata = tickets_store.load()
        ticket_info = tdata["open"].get(str(self.channel_id))
        return order, ticket_info

    @disnake.ui.button(emoji=icon("hand"), style=disnake.ButtonStyle.secondary, custom_id="shop_quote_work_claim", row=0)
    async def claim_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        order, ticket_info = self._load()
        if order is None or ticket_info is None:
            await inter.response.send_message("❌ Этот заказ или тикет уже закрыт.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("shop")):
            await inter.response.send_message("❌ Только персонал магазина может забирать заказы.", ephemeral=True)
            return
        if ticket_info.get("claimed_by") is not None:
            await inter.response.send_message(f"❌ Заказ уже забрал <@{ticket_info['claimed_by']}>.", ephemeral=True)
            return

        tdata = tickets_store.load()
        tdata["open"][str(self.channel_id)]["claimed_by"] = inter.author.id
        tickets_store.save(tdata)

        channel = inter.guild.get_channel(self.channel_id)
        panel_message_id = tdata["open"][str(self.channel_id)].get("panel_message_id")
        if channel is not None and panel_message_id:
            try:
                panel_message = await channel.fetch_message(panel_message_id)
                panel_embed = panel_message.embeds[0]
                for i, field in enumerate(panel_embed.fields):
                    if field.name == "Ответственный":
                        panel_embed.set_field_at(i, name="Ответственный", value=inter.author.mention, inline=True)
                        break
                await panel_message.edit(embed=panel_embed)
            except disnake.HTTPException:
                pass

        order, ticket_info = self._load()
        self.claim_button.disabled = True
        if order is not None and order["status"] == "awaiting_price":
            self.set_price_button.disabled = False
        await inter.response.edit_message(embed=_build_quote_work_embed(order, ticket_info), view=self)
        await _refresh_quote_list(inter.guild)

    @disnake.ui.button(emoji=icon("shield"), style=disnake.ButtonStyle.secondary, custom_id="shop_quote_work_admin_reply", row=0)
    async def admin_reply_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        order, ticket_info = self._load()
        if order is None or ticket_info is None:
            await inter.response.send_message("❌ Этот заказ или тикет уже закрыт.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("shop")):
            await inter.response.send_message("❌ Только персонал магазина может отвечать в этом тикете.", ephemeral=True)
            return
        channel = inter.guild.get_channel(self.channel_id)
        if channel is None:
            await inter.response.send_message("❌ Тикет этого заказа был удалён.", ephemeral=True)
            return
        await inter.response.send_modal(AdminReplyModal(target_channel=channel))

    @disnake.ui.button(label="Установить цену", emoji=icon("coin"), style=disnake.ButtonStyle.primary, custom_id="shop_quote_work_set_price", row=1)
    async def set_price_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        order, ticket_info = self._load()
        if order is None or order["status"] != "awaiting_price":
            await inter.response.send_message("❌ Этот заказ уже обработан.", ephemeral=True)
            return
        if not ticket_info or ticket_info.get("claimed_by") is None:
            await inter.response.send_message(
                f"❌ Сначала заберите заказ кнопкой {icon_tag('hand')} — без этого цену установить нельзя.",
                ephemeral=True,
            )
            return
        if ticket_info["claimed_by"] != inter.author.id and not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message(
                f"❌ Этот заказ ведёт <@{ticket_info['claimed_by']}> — цену может установить только он (или руководство).",
                ephemeral=True,
            )
            return
        await inter.response.send_modal(QuotePriceModal(self.order_id))

    @disnake.ui.button(emoji=icon("lock"), style=disnake.ButtonStyle.secondary, custom_id="shop_quote_work_close", row=1)
    async def close_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        order, ticket_info = self._load()
        if order is None or ticket_info is None:
            await inter.response.send_message("❌ Этот заказ или тикет уже закрыт.", ephemeral=True)
            return
        if not is_staff(inter.author, *_staff_role_ids("shop")):
            await inter.response.send_message("❌ Закрыть тикет может только персонал магазина.", ephemeral=True)
            return
        await inter.response.edit_message(
            content="Вы уверены, что хотите закрыть тикет? Канал будет удалён.",
            embed=None,
            view=QuoteCloseConfirmView(self.channel_id, self.order_id),
        )


class QuoteCloseConfirmView(disnake.ui.View):
    """Подтверждение закрытия — работает в том же эфемерном сообщении, что и QuoteWorkView,
    по той же схеме, что TicketWorkCloseConfirmView у заявок в семью."""

    def __init__(self, channel_id: int, order_id: int):
        super().__init__(timeout=120)
        self.channel_id = channel_id
        self.order_id = order_id

    @disnake.ui.button(label="Подтвердить закрытие", style=disnake.ButtonStyle.danger, custom_id="shop_quote_close_confirm")
    async def confirm(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = inter.guild.get_channel(self.channel_id)
        await inter.response.edit_message(
            content="🔒 Тикет закрывается, канал будет удалён через несколько секунд...", embed=None, view=None
        )

        transcript = await _build_transcript(channel) if channel is not None else None

        tdata = tickets_store.load()
        ticket_info = tdata["open"].pop(str(self.channel_id), None)
        tickets_store.save(tdata)

        log_ch_id = log_channel_id("tickets")
        if log_ch_id and channel is not None:
            log_channel = inter.guild.get_channel(log_ch_id)
            if log_channel is not None:
                await log_channel.send(embed=_build_close_log_embed(channel, ticket_info, inter.author), file=transcript)

        pdata = payments_store.load()
        for stored in pdata["orders"]:
            if stored["id"] == self.order_id and stored["status"] != "paid":
                stored["status"] = "cancelled"
                break
        payments_store.save(pdata)

        await _refresh_quote_list(inter.guild)

        if channel is not None:
            await asyncio.sleep(5)
            try:
                await channel.delete(reason=f"Тикет закрыт из модерации пользователем {inter.author}")
            except disnake.HTTPException:
                pass

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="shop_quote_close_cancel")
    async def cancel(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        pdata = payments_store.load()
        order = next((o for o in pdata["orders"] if o["id"] == self.order_id), None)
        if order is None:
            await inter.response.edit_message(content="Закрытие отменено. Заказ уже недоступен.", embed=None, view=None)
            return
        tdata = tickets_store.load()
        ticket_info = tdata["open"].get(str(self.channel_id))
        await inter.response.edit_message(
            content=None, embed=_build_quote_work_embed(order, ticket_info), view=QuoteWorkView(order, ticket_info)
        )


class QuoteOrderSelect(disnake.ui.StringSelect):
    def __init__(self, orders: list[dict]):
        if orders:
            options = [
                disnake.SelectOption(
                    label=f"№{o['id']} — {o['service_label']}"[:100],
                    value=str(o["id"]),
                    description=QUOTE_STATUS_LABEL.get(o["status"], ("", o["status"]))[1][:100],
                    emoji=icon("coin"),
                )
                for o in orders[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет активных заказов", value="none")]
        super().__init__(placeholder="Выберите заказ, чтобы им заняться", options=options, custom_id="shop_quote_list_select")

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет активных заказов «по сложности».", ephemeral=True)
            return

        order_id = int(self.values[0])
        data = payments_store.load()
        order = next((o for o in data["orders"] if o["id"] == order_id), None)
        if order is None:
            await inter.response.send_message("❌ Этот заказ не найден.", ephemeral=True)
            await _refresh_quote_list(inter.guild)
            return
        if not is_staff(inter.author, *_staff_role_ids("shop")):
            await inter.response.send_message("❌ Только персонал магазина может обрабатывать эти заказы.", ephemeral=True)
            return

        ticket_info = _quote_ticket_info(order)
        await inter.response.send_message(
            embed=_build_quote_work_embed(order, ticket_info), view=QuoteWorkView(order, ticket_info), ephemeral=True
        )


class QuoteListView(disnake.ui.View):
    def __init__(self, orders: list[dict]):
        super().__init__(timeout=None)
        self.add_item(QuoteOrderSelect(orders))


async def _refresh_quote_list(guild: disnake.Guild) -> None:
    """Поддерживает единственное сообщение со списком заказов «по сложности» в канале
    модерации — создаёт при первой необходимости, дальше только редактирует. Канал берётся
    из того, что задали командой /shop moderation, а если её ни разу не вызывали — из
    статического shop.quote_moderation_channel_id в config.json."""
    data = payments_store.load()
    channel_id = data.get("quote_list_channel_id") or config.get("shop.quote_moderation_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return

    orders = _quote_orders(guild)
    embed = _build_quote_list_embed(orders)
    view = QuoteListView(orders)

    message_id = data.get("quote_list_message_id")
    message = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=view)
        except disnake.HTTPException:
            message = None
    if message is None:
        message = await channel.send(embed=embed, view=view)
        data = payments_store.load()
        data["quote_list_message_id"] = message.id
        payments_store.save(data)


def _order_duration_text(order: dict) -> str | None:
    created_at = order.get("created_at")
    paid_at = order.get("paid_at")
    if not created_at or not paid_at:
        return None
    try:
        started = dt.datetime.fromisoformat(created_at)
        finished = dt.datetime.fromisoformat(paid_at)
    except ValueError:
        return None
    total_minutes = max(0, int((finished - started).total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


async def _log_payment(
    guild: disnake.Guild,
    order: dict,
    channel: disnake.TextChannel | None,
    member: disnake.Member | None = None,
    confirmed_by: disnake.Member | None = None,
) -> None:
    """Лог оплаты со всем, что может понадобиться для проверки задним числом: кто, что,
    сколько, через какого провайдера, каким ID платежа, кто и как подтвердил, сколько
    времени заняло с оформления заказа до оплаты."""
    channel_id = log_channel_id("shop")
    if not channel_id:
        return
    log_channel = guild.get_channel(channel_id)
    if log_channel is None:
        return

    is_quote = order.get("task_description") is not None
    provider = "ЮKassa" if order.get("yookassa_payment_id") else "ЮMoney"

    embed = base_embed(f"{SHOP_EMOJI['tag']} Оплата получена", color=disnake.Color.green(), timestamp=True)
    if member is not None:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="Номер заказа", value=f"№{order['id']}", inline=True)
    embed.add_field(name="Тип заказа", value="По сложности" if is_quote else "Каталог", inline=True)
    embed.add_field(name="Сумма", value=_fmt_amount(order["amount"]), inline=True)

    embed.add_field(name="Услуга", value=order["service_label"], inline=True)
    embed.add_field(name="Покупатель", value=f"<@{order['user_id']}>\n`{order['user_id']}`", inline=True)
    embed.add_field(name="Тикет", value=channel.mention if channel is not None else "не создан", inline=True)

    embed.add_field(name="Провайдер оплаты", value=provider, inline=True)
    embed.add_field(name="Метка заказа", value=f"`{order['label']}`", inline=True)
    if order.get("yookassa_payment_id"):
        embed.add_field(name="ID платежа ЮKassa", value=f"`{order['yookassa_payment_id']}`", inline=True)
    embed.add_field(
        name="Как подтверждено",
        value=f"Вручную — {confirmed_by.mention}" if confirmed_by is not None else "Автоматически",
        inline=True,
    )

    if is_quote:
        priced_by = order.get("priced_by")
        embed.add_field(name="Цену назначил", value=f"<@{priced_by}>" if priced_by else "—", inline=True)
        duration = _order_duration_text(order)
        embed.add_field(name="Оформлен → оплачен", value=duration or "—", inline=True)
        embed.add_field(name="Задание", value=(order.get("task_description") or "—")[:1024], inline=False)
    else:
        duration = _order_duration_text(order)
        if duration:
            embed.add_field(name="Оформлен → оплачен", value=duration, inline=True)

    await log_channel.send(embed=embed)


async def _process_paid_order(
    bot: commands.InteractionBot, order: dict, confirmed_by: disnake.Member | None = None
) -> None:
    guild = bot.get_guild(order["guild_id"])
    if guild is None:
        return
    member = guild.get_member(order["user_id"])
    if member is None:
        return

    service = _find_service(order["service_key"]) or {
        "key": order["service_key"],
        "label": order["service_label"],
        "icon": "cart",
    }

    # Заказ «по сложности» уже открыл тикет заранее (до того, как узнал цену) — используем
    # тот же канал вместо создания нового.
    is_quote = bool(order.get("ticket_channel_id"))
    if is_quote:
        channel = guild.get_channel(order["ticket_channel_id"])
    else:
        channel = await _create_ticket_channel(
            _InteractionStub(guild, member),
            "shop",
            None,
            category_label=order["service_label"],
            extra_fields=[
                ("Услуга", order["service_label"]),
                ("Оплачено", f"{order['amount']:,.0f} ₽".replace(",", " ")),
                ("Номер заказа", f"№{order['id']}"),
            ],
            name_suffix=order["service_key"],
            embed_title=f"{_service_icon(service)} Заказ №{order['id']}: {order['service_label']}",
        )

    data = payments_store.load()
    stored = next((o for o in data["orders"] if o["id"] == order["id"]), None)
    if stored is not None:
        stored["status"] = "paid"
        stored["paid_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if channel is not None:
            stored["ticket_channel_id"] = channel.id
        payments_store.save(data)
        order = stored  # дальше используем актуальные status/paid_at — нужны логу для длительности

    if is_quote and channel is not None:
        paid_embed = base_embed(
            f"{icon_tag('check')} Оплата получена",
            f"Спасибо! Оплата по заказу **{order['service_label']}** получена — можно приступать к работе.",
            color=disnake.Color.green(),
        )
        paid_embed.add_field(name="Сумма", value=f"{order['amount']:,.0f} ₽".replace(",", " "), inline=True)
        await channel.send(embed=paid_embed)
        await _refresh_quote_list(guild)

    try:
        if channel is not None:
            dm_embed = base_embed(
                f"{icon_tag('check')} Оплата получена",
                f"Спасибо за оплату услуги **{order['service_label']}**! Загляните в тикет — команда уже видит подтверждение.",
                color=disnake.Color.green(),
            )
            dm_embed.add_field(name="Канал", value=channel.mention, inline=False)
            await member.send(embed=dm_embed)
        else:
            dm_embed = base_embed(
                f"{icon_tag('alert')} Оплата получена, но тикет не создан",
                f"Оплата за услугу **{order['service_label']}** получена, но бот не смог найти или создать "
                "тикет-канал — напишите руководству, чтобы разобраться.",
                color=disnake.Color.orange(),
            )
            await member.send(embed=dm_embed)
    except disnake.Forbidden:
        pass

    await _log_payment(guild, order, channel, member=member, confirmed_by=confirmed_by)


async def _try_claim_order(order_id: int) -> dict | None:
    """Атомарно (в рамках одного процесса) помечает pending-заказ как processing и
    возвращает его — или None, если заказ уже обработан где-то ещё (вебхук, /shop
    payments вручную, второй цикл опроса). Так один и тот же платёж не создаст два тикета."""
    data = payments_store.load()
    current = next((o for o in data["orders"] if o["id"] == order_id), None)
    if current is None or current["status"] != "pending":
        return None
    current["status"] = "processing"
    payments_store.save(data)
    return current


async def _poll_payments(bot: commands.InteractionBot) -> None:
    data = payments_store.load()
    now = dt.datetime.now(dt.timezone.utc)

    # Заказы «по сложности» держат ticket_channel_id ещё до оплаты (обычные заказы —
    # только после неё) — если такой тикет закрыли/удалили вручную, не дожидаясь оплаты,
    # заказ не должен вечно висеть в awaiting_price/pending.
    orphaned = False
    orphaned_guild_ids: set[int] = set()
    for o in data["orders"]:
        if o["status"] not in ("awaiting_price", "pending") or not o.get("ticket_channel_id"):
            continue
        guild = bot.get_guild(o["guild_id"])
        if guild is not None and guild.get_channel(o["ticket_channel_id"]) is None:
            o["status"] = "cancelled"
            orphaned = True
            orphaned_guild_ids.add(o["guild_id"])
    if orphaned:
        payments_store.save(data)
        for guild_id in orphaned_guild_ids:
            guild = bot.get_guild(guild_id)
            if guild is not None:
                await _refresh_quote_list(guild)

    expired_ids = [
        o["id"]
        for o in data["orders"]
        if o["status"] == "pending"
        and (now - dt.datetime.fromisoformat(o["created_at"])).total_seconds() > ORDER_EXPIRY_MINUTES * 60
    ]
    if expired_ids:
        for o in data["orders"]:
            if o["id"] in expired_ids:
                o["status"] = "expired"
        payments_store.save(data)

    pending = [o for o in data["orders"] if o["status"] == "pending"]
    if not pending:
        return

    # ЮKassa: подстраховка на случай, если мгновенный вебхук через Cloudflare не долетел.
    # Запросы статуса уходят параллельно (asyncio.gather) — иначе N заказов ждали бы
    # N последовательных round-trip'ов к API ЮKassa один за другим; сама заявка на
    # обработку (claim/process) при этом остаётся последовательной.
    yookassa_pending = [o for o in pending if o.get("yookassa_payment_id")]
    if yookassa_pending:
        payments = await asyncio.gather(
            *(yookassa_get_payment(o["yookassa_payment_id"]) for o in yookassa_pending)
        )
        for order, payment in zip(yookassa_pending, payments):
            if payment is None or payment.get("status") != "succeeded":
                continue
            claimed = await _try_claim_order(order["id"])
            if claimed is not None:
                await _process_paid_order(bot, claimed)

    # ЮMoney: сверка по истории операций кошелька (для заказов без привязки к ЮKassa).
    yoomoney_pending = [o for o in pending if not o.get("yookassa_payment_id")]
    if not yoomoney_pending:
        return

    operations = await fetch_recent_operations(records=50)
    if not operations:
        return
    by_label = {op.get("label"): op for op in operations if op.get("label")}

    for order in yoomoney_pending:
        op = by_label.get(order["label"])
        if op is None or op.get("status") != "success":
            continue
        if float(op.get("amount", 0)) < order["amount"] - 0.01:
            continue  # недоплата — оставляем висеть, вдруг доплатят вторым переводом

        claimed = await _try_claim_order(order["id"])
        if claimed is not None:
            await _process_paid_order(bot, claimed)


async def _handle_yookassa_relay_message(bot: commands.InteractionBot, content: str) -> None:
    """Сообщение из служебного канала-трубы: Cloudflare Worker пересылает сюда события
    вебхука ЮKassa в формате "yookassa:<event>:<payment_id>". Само тело вебхука здесь
    не используется — оно ничем не подписано и ему нельзя доверять напрямую. Мы берём
    из сообщения только payment_id и идём за реальным статусом прямо в API ЮKassa."""
    parts = content.split(":", 2)
    if len(parts) != 3 or parts[0] != "yookassa":
        return
    _, _event, payment_id = parts

    payment = await yookassa_get_payment(payment_id)
    if payment is None or payment.get("status") != "succeeded":
        return

    order_id_raw = (payment.get("metadata") or {}).get("order_id")
    if not order_id_raw:
        return
    try:
        order_id = int(order_id_raw)
    except ValueError:
        return

    order = await _try_claim_order(order_id)
    if order is not None:
        await _process_paid_order(bot, order)


class ManualConfirmSelect(disnake.ui.StringSelect):
    def __init__(self, orders: list[dict]):
        options = [
            disnake.SelectOption(
                label=f"№{o['id']} — {o['service_label']} — {o['amount']:,.0f} ₽".replace(",", " ")[:100],
                value=str(o["id"]),
                description=f"<@{o['user_id']}>"[:100],
            )
            for o in orders[:25]
        ]
        super().__init__(placeholder="Подтвердить оплату вручную", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        order_id = int(self.values[0])
        order = await _try_claim_order(order_id)
        if order is None:
            await inter.response.edit_message(content="❌ Этот заказ уже обработан или не найден.", view=None)
            return

        await inter.response.edit_message(content=f"⏳ Подтверждаю заказ №{order_id} вручную...", view=None)
        await _process_paid_order(inter.bot, order, confirmed_by=inter.author)
        await inter.followup.send(f"✅ Заказ №{order_id} подтверждён вручную и тикет открыт.", ephemeral=True)


class ManualConfirmView(disnake.ui.View):
    def __init__(self, orders: list[dict]):
        super().__init__(timeout=180)
        self.add_item(ManualConfirmSelect(orders))


class Shop(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.check_payments.start()

    def cog_unload(self):
        self.check_payments.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def check_payments(self) -> None:
        await _poll_payments(self.bot)

    @check_payments.before_loop
    async def _before_check_payments(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if not WEBHOOK_RELAY_CHANNEL_ID or message.channel.id != WEBHOOK_RELAY_CHANNEL_ID:
            return
        if message.webhook_id is None:
            return  # только сообщения от настоящего вебхука, не от людей в канале
        await _handle_yookassa_relay_message(self.bot, message.content)

    @commands.slash_command(name="shop", description="Магазин услуг семьи")
    async def shop(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @shop.sub_command(name="show", description="Опубликовать панель магазина услуг (только для персонала)")
    async def shop_show(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав для публикации панели магазина.", ephemeral=True)
            return

        message = await send_panel(inter, _build_shop_embed(), view=ShopPanelView(), panel_key="shop")

        data = shop_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        shop_store.save(data)

    @shop.sub_command(name="adm", description="Управление услугами магазина: добавление, изменение, удаление (только для персонала)")
    async def shop_adm(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав для управления магазином.", ephemeral=True)
            return

        embed = base_embed(
            f"{SHOP_EMOJI['cart']} Управление магазином услуг",
            "Добавляйте, меняйте и убирайте услуги. Покупатели видят и заказывают их через "
            "`/shop show` — панель обновится сама после любого изменения.",
        )
        embed.add_field(name="Кнопки", value="➕ Добавить · ✏️ Изменить · 🗑️ Удалить", inline=False)
        await inter.response.send_message(embed=embed, view=ShopAdminView(), ephemeral=True)

    @shop.sub_command(name="payments", description="Заказы, ожидающие оплаты — на случай, если авто-проверка не сработала (только для персонала)")
    async def shop_payments(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав.", ephemeral=True)
            return

        data = payments_store.load()
        pending = [o for o in data["orders"] if o["status"] == "pending"]
        if not pending:
            await inter.response.send_message("Сейчас нет заказов, ожидающих оплаты.", ephemeral=True)
            return

        lines = [
            f"№{o['id']} — **{o['service_label']}** — {o['amount']:,.0f} ₽ — <@{o['user_id']}> — метка `{o['label']}`".replace(",", " ")
            for o in pending
        ]
        embed = base_embed(
            f"{SHOP_EMOJI['tag']} Ожидающие оплаты ({len(pending)})",
            "Бот проверяет их автоматически каждые "
            f"{POLL_SECONDS} сек. Если оплата уже пришла, а тикет не открылся (например, "
            "недоплата или платёж без метки) — подтвердите заказ вручную в меню ниже.",
        )
        embed.add_field(name="Заказы", value="\n".join(lines)[:4000], inline=False)
        await inter.response.send_message(embed=embed, view=ManualConfirmView(pending), ephemeral=True)

    @shop.sub_command(name="moderation", description="Опубликовать панель модерации заказов «по сложности» в этом канале (только для персонала)")
    async def shop_moderation(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав.", ephemeral=True)
            return

        await inter.response.send_message("✅ Панель опубликована в этом канале.", ephemeral=True)

        data = payments_store.load()
        data["quote_list_channel_id"] = inter.channel.id
        data["quote_list_message_id"] = None
        payments_store.save(data)

        await _refresh_quote_list(inter.guild)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Shop(bot))


PERSISTENT_VIEWS = [
    lambda: ShopAdminView(),
    lambda: ShopPanelView(),
    lambda: QuoteListView(_quote_orders_unchecked()),
]
