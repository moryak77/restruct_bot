from __future__ import annotations

import datetime as dt

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.checks import is_staff
from core.config import config, log_channel_id
from core.icons import icon, icon_tag
from core.storage import bank_store

ADMIN_PERMS = disnake.Permissions(manage_guild=True)
HISTORY_SHOWN = 20


def _withdraw_role_ids() -> list[int]:
    return [i for i in (config.get("bank.withdraw_role_ids", []) or []) if i]


def _can_withdraw(member: disnake.Member) -> bool:
    return is_staff(member, *_withdraw_role_ids())


def _fmt_money(amount: int) -> str:
    return f"${amount:,}".replace(",", " ")


def _parse_amount(raw: str) -> int | None:
    cleaned = raw.strip().replace(" ", "").replace(",", "").replace("$", "")
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if 0 < value <= 1_000_000_000_000 else None


def _build_bank_embed(data: dict) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('bank')} Банк семьи",
        "Общая казна семьи. Пополнить баланс может любой участник, а снять средства — "
        "только руководство. Это исключает путаницу и злоупотребления.",
        panel_key="bank",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            f"{icon_tag('increase')} — **Пополнить** — доступно всем участникам семьи.\n"
            f"{icon_tag('decrease')} — **Снять** — только руководству (Owner, Dep. Owner).\n"
            "Каждая операция сразу попадает в `/history_bank` и в канал логов — баланс "
            "всегда прозрачен и его легко проверить."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{icon_tag('coin')} Текущий баланс",
        value=f"**{_fmt_money(data.get('balance', 0))}**",
        inline=False,
    )
    embed.add_field(
        name=f"{icon_tag('clipboard')} История операций",
        value="Используйте команду `/history_bank`, чтобы посмотреть последние операции.",
        inline=False,
    )

    embed.set_footer(text="Полная история хранится в канале логов • Семья RESTRUCT")
    return embed


def _format_tx_line(tx: dict) -> str:
    is_deposit = tx["type"] == "deposit"
    line_icon = icon_tag("increase") if is_deposit else icon_tag("decrease")
    sign = "+" if is_deposit else "−"
    ts_raw = tx.get("timestamp")
    time_part = ""
    if ts_raw:
        try:
            ts = int(dt.datetime.fromisoformat(ts_raw).timestamp())
            time_part = f" · <t:{ts}:f>"
        except ValueError:
            pass
    reason = f" — _{tx['reason']}_" if tx.get("reason") else ""
    return f"{line_icon} **{sign}{_fmt_money(tx['amount'])}** · <@{tx['author_id']}>{reason}{time_part}"


def _build_history_embed(data: dict) -> disnake.Embed:
    embed = base_embed(f"{icon_tag('clipboard')} История операций банка")

    transactions = data.get("transactions", [])
    recent = list(reversed(transactions[-HISTORY_SHOWN:]))
    if not recent:
        embed.description = "Пока нет операций."
        return embed

    embed.description = "\n".join(_format_tx_line(tx) for tx in recent)
    embed.set_footer(
        text=f"Показано {len(recent)} из {len(transactions)} • Полная история хранится в канале логов"
    )
    return embed


async def _refresh_bank_panel(guild: disnake.Guild) -> None:
    data = bank_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    embed = _build_bank_embed(data)
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=BankPanelView())
    except disnake.HTTPException:
        pass


async def _log_transaction(guild: disnake.Guild, tx: dict, new_balance: int) -> None:
    channel_id = log_channel_id("bank")
    if not channel_id:
        return
    log_channel = guild.get_channel(channel_id)
    if log_channel is None:
        return

    is_deposit = tx["type"] == "deposit"
    title = f"{icon_tag('increase')} Пополнение банка семьи" if is_deposit else f"{icon_tag('decrease')} Снятие с банка семьи"
    embed = base_embed(title, color=disnake.Color.dark_grey())
    embed.add_field(name="Сумма", value=_fmt_money(tx["amount"]), inline=True)
    embed.add_field(name="Участник", value=f"<@{tx['author_id']}>", inline=True)
    embed.add_field(name="Новый баланс", value=_fmt_money(new_balance), inline=True)
    if tx.get("reason"):
        embed.add_field(name="Комментарий", value=tx["reason"], inline=False)
    await log_channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Пополнение / снятие
# ---------------------------------------------------------------------------

class DepositModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Сумма",
                custom_id="amount",
                style=disnake.TextInputStyle.short,
                max_length=15,
                placeholder="Например: 15000",
            ),
            disnake.ui.TextInput(
                label="Комментарий (необязательно)",
                custom_id="reason",
                style=disnake.TextInputStyle.short,
                max_length=200,
                required=False,
            ),
        ]
        super().__init__(title="Пополнить банк семьи", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        amount = _parse_amount(inter.text_values["amount"])
        if amount is None:
            await inter.response.send_message("❌ Укажите положительное целое число, например 15000.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)

        data = bank_store.load()
        data["balance"] = data.get("balance", 0) + amount
        tx = {
            "id": data["next_id"],
            "type": "deposit",
            "amount": amount,
            "author_id": inter.author.id,
            "reason": inter.text_values["reason"].strip() or None,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        data["transactions"].append(tx)
        data["next_id"] += 1
        bank_store.save(data)

        await inter.followup.send(f"✅ Баланс пополнен на {_fmt_money(amount)}.", ephemeral=True)
        await _refresh_bank_panel(inter.guild)
        await _log_transaction(inter.guild, tx, data["balance"])


class WithdrawModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Сумма",
                custom_id="amount",
                style=disnake.TextInputStyle.short,
                max_length=15,
                placeholder="Например: 15000",
            ),
            disnake.ui.TextInput(
                label="Комментарий (необязательно)",
                custom_id="reason",
                style=disnake.TextInputStyle.short,
                max_length=200,
                required=False,
            ),
        ]
        super().__init__(title="Снять с банка семьи", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        amount = _parse_amount(inter.text_values["amount"])
        if amount is None:
            await inter.response.send_message("❌ Укажите положительное целое число, например 15000.", ephemeral=True)
            return

        data = bank_store.load()
        balance = data.get("balance", 0)
        if amount > balance:
            await inter.response.send_message(
                f"❌ Недостаточно средств: в банке {_fmt_money(balance)}, вы запросили {_fmt_money(amount)}.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        data["balance"] = balance - amount
        tx = {
            "id": data["next_id"],
            "type": "withdraw",
            "amount": amount,
            "author_id": inter.author.id,
            "reason": inter.text_values["reason"].strip() or None,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        data["transactions"].append(tx)
        data["next_id"] += 1
        bank_store.save(data)

        await inter.followup.send(f"✅ Снято {_fmt_money(amount)}.", ephemeral=True)
        await _refresh_bank_panel(inter.guild)
        await _log_transaction(inter.guild, tx, data["balance"])


class BankPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Пополнить", emoji=icon("increase"), style=disnake.ButtonStyle.secondary, custom_id="bank_deposit")
    async def deposit_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(DepositModal())

    @disnake.ui.button(label="Снять", emoji=icon("decrease"), style=disnake.ButtonStyle.secondary, custom_id="bank_withdraw")
    async def withdraw_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not _can_withdraw(inter.author):
            await inter.response.send_message(
                "❌ Снимать средства может только руководство (Owner, Dep. Owner).", ephemeral=True
            )
            return
        await inter.response.send_modal(WithdrawModal())


# ---------------------------------------------------------------------------
# Слэш-команда
# ---------------------------------------------------------------------------

class Bank(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(
        name="bank",
        description="Показать панель банка семьи",
        default_member_permissions=ADMIN_PERMS,
    )
    async def bank(self, inter: disnake.ApplicationCommandInteraction):
        data = bank_store.load()
        embed = _build_bank_embed(data)
        message = await send_panel(inter, embed, view=BankPanelView(), panel_key="bank")

        data = bank_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        bank_store.save(data)

    @commands.slash_command(
        name="history_bank",
        description="Показать историю операций банка семьи",
    )
    async def history_bank(self, inter: disnake.ApplicationCommandInteraction):
        data = bank_store.load()
        embed = _build_history_embed(data)
        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Bank(bot))


PERSISTENT_VIEWS = [lambda: BankPanelView()]
