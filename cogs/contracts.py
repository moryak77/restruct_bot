from __future__ import annotations

import datetime as dt
import re

import disnake
from disnake.ext import commands, tasks

from core.branding import base_embed, send_panel
from core.checks import is_staff
from core.config import config, log_channel_id
from core.icons import icon, icon_tag
from core.storage import contracts_store

MIN_MEMBERS = 2
MAX_MEMBERS = 30
MAX_DURATION_MINUTES = 2 * 60 + 30  # 2:30
EXPIRY_CHECK_MINUTES = 5
CONTRACTS_CHUNK_SIZE = 10

_DURATION_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def _contract_role_ids() -> list[int]:
    return [i for i in (config.get("contracts.role_ids", []) or []) if i]


def _can_handle_contracts(member: disnake.Member) -> bool:
    """Создавать и брать контракты может только тот, у кого есть роль из contracts.role_ids."""
    return is_staff(member, *_contract_role_ids())


def _role_mention() -> str:
    role_ids = _contract_role_ids()
    return f"<@&{role_ids[0]}>" if role_ids else "нужной ролью"


def _parse_headcount(raw: str) -> int | None:
    cleaned = raw.strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if MIN_MEMBERS <= value <= MAX_MEMBERS else None


def _parse_duration_minutes(raw: str) -> int | None:
    match = _DURATION_RE.match(raw.strip())
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    total = hours * 60 + minutes
    return total if 0 < total <= MAX_DURATION_MINUTES else None


def _parse_percent(raw: str) -> int | None:
    cleaned = raw.strip().replace("%", "")
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if 1 <= value <= 100 else None


def _find_contract(data: dict, contract_id: int) -> dict | None:
    return next((c for c in data["contracts"] if c["id"] == contract_id), None)


def _find_active_contract_for(data: dict, user_id: int) -> dict | None:
    """Контракт, в котором участник уже состоит (как создатель или как участник)."""
    return next(
        (c for c in data["contracts"] if c["creator_id"] == user_id or user_id in c.get("members", [])),
        None,
    )


def _timestamp(iso_value: str | None) -> int | None:
    if not iso_value:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso_value).timestamp())
    except ValueError:
        return None


def _build_contracts_embed(data: dict) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('package')} Контракты семьи",
        f"Создавать и брать контракты может только тот, у кого есть роль {_role_mention()}. "
        "Вексели за выполненный контракт выдаются организации и забираются лично в офисе.",
        panel_key="contracts",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            f"{icon_tag('increase')} — **Взять контракт** — выберите в меню, если у вас есть "
            f"роль {_role_mention()}.\n"
            f"{icon_tag('delete')} — **Закрыть контракт** — может только создатель контракта. "
            "Выйти из уже взятого контракта нельзя.\n"
            f"{icon_tag('kick')} — **Кикнуть с контракта** — создатель может исключить участника, "
            "который передумал или не справляется, освободив место для другого.\n"
            "Если создатель не закроет контракт к истечению времени выполнения — его тегнёт бот.\n"
            "Создатель сразу считается одним из участников контракта. "
            "Один человек одновременно может состоять только в одном контракте — своём или чужом.\n"
            "Чем выше указанный процент — тем меньше векселей падает всего; "
            "чем ниже процент — тем больше общая выплата."
        ),
        inline=False,
    )

    contracts = data.get("contracts", [])
    if not contracts:
        embed.add_field(name=f"{icon_tag('clipboard')} Открытые контракты", value="Пока нет ни одного контракта.", inline=False)
        return embed

    for i, start in enumerate(range(0, len(contracts), CONTRACTS_CHUNK_SIZE)):
        chunk = contracts[start:start + CONTRACTS_CHUNK_SIZE]
        lines = []
        for c in chunk:
            members = c.get("members", [])
            full = len(members) >= c["max_members"]
            status = "🔒" if full else "🟢"
            lines.append(
                f"{status} **{c['title']}** — {len(members)}/{c['max_members']} · "
                f"{c['duration']} · {c['percent']}% · создал <@{c['creator_id']}>"
            )
        name = f"{icon_tag('clipboard')} Контракты ({len(contracts)})" if i == 0 else f"{icon_tag('clipboard')} Контракты (продолжение)"
        embed.add_field(name=name, value="\n".join(lines)[:1024], inline=False)

    return embed


async def _refresh_contracts_panel(guild: disnake.Guild) -> None:
    data = contracts_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    embed = _build_contracts_embed(data)
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=_build_contracts_panel_view())
    except disnake.HTTPException:
        pass


async def _log_event(guild: disnake.Guild, title: str, fields: list[tuple[str, str]]) -> None:
    channel_id = log_channel_id("contracts")
    if not channel_id:
        return
    log_channel = guild.get_channel(channel_id)
    if log_channel is None:
        return

    embed = base_embed(title, color=disnake.Color.dark_grey())
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=True)
    await log_channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Создание контракта
# ---------------------------------------------------------------------------

class CreateContractModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Название контракта",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=100,
                placeholder="Например: Поставка металла",
            ),
            disnake.ui.TextInput(
                label="Время выполнения (чч:мм, максимум 2:30)",
                custom_id="duration",
                style=disnake.TextInputStyle.short,
                max_length=5,
                placeholder="Например: 2:30",
            ),
            disnake.ui.TextInput(
                label="Сколько всего человек (минимум 2)",
                custom_id="max_members",
                style=disnake.TextInputStyle.short,
                max_length=3,
                placeholder="Например: 4",
            ),
            disnake.ui.TextInput(
                label="Процент (1-100)",
                custom_id="percent",
                style=disnake.TextInputStyle.short,
                max_length=3,
                placeholder="Например: 100",
            ),
        ]
        super().__init__(title="Создание контракта", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        title = inter.text_values["title"].strip()
        duration_raw = inter.text_values["duration"].strip()
        duration_minutes = _parse_duration_minutes(duration_raw)
        max_members = _parse_headcount(inter.text_values["max_members"])
        percent = _parse_percent(inter.text_values["percent"])

        if not title:
            await inter.response.send_message("❌ Укажите название контракта.", ephemeral=True)
            return
        if duration_minutes is None:
            await inter.response.send_message(
                "❌ Укажите время выполнения в формате чч:мм, например 2:30. Максимум — 2:30.",
                ephemeral=True,
            )
            return
        if max_members is None:
            await inter.response.send_message(
                f"❌ Количество человек должно быть целым числом от {MIN_MEMBERS} до {MAX_MEMBERS}.", ephemeral=True
            )
            return
        if percent is None:
            await inter.response.send_message("❌ Процент должен быть целым числом от 1 до 100.", ephemeral=True)
            return

        data = contracts_store.load()
        active = _find_active_contract_for(data, inter.author.id)
        if active is not None:
            await inter.response.send_message(
                f"❌ Вы уже состоите в контракте **{active['title']}** — сначала он должен быть закрыт.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)

        now = dt.datetime.now(dt.timezone.utc)
        data = contracts_store.load()
        contract = {
            "id": data["next_id"],
            "title": title,
            "duration": duration_raw,
            "max_members": max_members,
            "percent": percent,
            "members": [inter.author.id],
            "creator_id": inter.author.id,
            "created_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(minutes=duration_minutes)).isoformat(),
            "expiry_notified": False,
        }
        data["contracts"].append(contract)
        data["next_id"] += 1
        contracts_store.save(data)

        await inter.followup.send(f"✅ Контракт **{title}** создан.", ephemeral=True)
        await _refresh_contracts_panel(inter.guild)
        await _log_event(
            inter.guild,
            f"{icon_tag('package')} Новый контракт",
            [
                ("Название", title),
                ("Время выполнения", duration_raw),
                ("Мест", f"{max_members}"),
                ("Процент", f"{percent}%"),
                ("Создал", f"<@{inter.author.id}>"),
            ],
        )


# ---------------------------------------------------------------------------
# Взять контракт
# ---------------------------------------------------------------------------

class TakeContractSelect(disnake.ui.StringSelect):
    def __init__(self, contracts: list[dict]):
        open_contracts = [c for c in contracts if len(c.get("members", [])) < c["max_members"]]
        if open_contracts:
            options = [
                disnake.SelectOption(
                    label=f"{c['title']} — {len(c['members'])}/{c['max_members']}"[:100],
                    value=str(c["id"]),
                    description=f"{c['duration']} · {c['percent']}%"[:100],
                )
                for c in open_contracts[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет открытых контрактов", value="none")]
        super().__init__(placeholder="✅ Взять контракт", options=options, custom_id="contracts_take_select")

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет открытых контрактов.", ephemeral=True)
            return

        if not _can_handle_contracts(inter.author):
            await inter.response.send_message(
                f"❌ Брать контракты может только тот, у кого есть роль {_role_mention()}.", ephemeral=True
            )
            return

        contract_id = int(self.values[0])
        data = contracts_store.load()
        contract = _find_contract(data, contract_id)
        if contract is None:
            await inter.response.send_message("❌ Этот контракт больше не существует.", ephemeral=True)
            await _refresh_contracts_panel(inter.guild)
            return

        active = _find_active_contract_for(data, inter.author.id)
        if active is not None:
            await inter.response.send_message(
                f"❌ Вы уже состоите в контракте **{active['title']}** — сначала он должен быть закрыт.",
                ephemeral=True,
            )
            return
        if len(contract["members"]) >= contract["max_members"]:
            await inter.response.send_message("❌ Набор на этот контракт уже завершён.", ephemeral=True)
            await _refresh_contracts_panel(inter.guild)
            return

        contract["members"].append(inter.author.id)
        contracts_store.save(data)

        await inter.response.send_message(f"✅ Вы взяли контракт **{contract['title']}**.", ephemeral=True)
        await _refresh_contracts_panel(inter.guild)


class CreateContractButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(label="Создать контракт", style=disnake.ButtonStyle.secondary, emoji=icon("increase"), custom_id="contracts_create")

    async def callback(self, inter: disnake.MessageInteraction):
        if not _can_handle_contracts(inter.author):
            await inter.response.send_message(f"❌ Создавать контракты может только тот, у кого есть роль {_role_mention()}.", ephemeral=True)
            return

        active = _find_active_contract_for(contracts_store.load(), inter.author.id)
        if active is not None:
            await inter.response.send_message(
                f"❌ Вы уже состоите в контракте **{active['title']}** — сначала он должен быть закрыт.",
                ephemeral=True,
            )
            return

        await inter.response.send_modal(CreateContractModal())


def _can_close(member: disnake.Member, contract: dict) -> bool:
    return contract["creator_id"] == member.id or member.guild_permissions.manage_guild


class CloseContractSelect(disnake.ui.StringSelect):
    def __init__(self, contracts: list[dict]):
        options = [
            disnake.SelectOption(
                label=f"{c['title']} — {len(c['members'])}/{c['max_members']}"[:100],
                value=str(c["id"]),
            )
            for c in contracts[:25]
        ]
        super().__init__(placeholder="Выберите контракт для закрытия", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        contract_id = int(self.values[0])
        data = contracts_store.load()
        contract = _find_contract(data, contract_id)
        if contract is None:
            await inter.response.edit_message(content="❌ Контракт не найден, возможно уже закрыт.", view=None)
            return
        if not _can_close(inter.author, contract):
            await inter.response.send_message("❌ Закрыть контракт может только его создатель.", ephemeral=True)
            return

        data["contracts"] = [c for c in data["contracts"] if c["id"] != contract_id]
        contracts_store.save(data)

        await inter.response.edit_message(content=f"{icon_tag('delete')} Контракт **{contract['title']}** закрыт.", view=None)
        await _refresh_contracts_panel(inter.guild)
        await _log_event(
            inter.guild,
            f"{icon_tag('delete')} Контракт закрыт",
            [("Название", contract["title"]), ("Закрыл", f"<@{inter.author.id}>")],
        )


class CloseContractView(disnake.ui.View):
    def __init__(self, contracts: list[dict]):
        super().__init__(timeout=120)
        self.add_item(CloseContractSelect(contracts))


class CloseContractButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(label="Закрыть контракт", style=disnake.ButtonStyle.secondary, emoji=icon("delete"), custom_id="contracts_close")

    async def callback(self, inter: disnake.MessageInteraction):
        contracts = contracts_store.load()["contracts"]
        eligible = contracts if inter.author.guild_permissions.manage_guild else [
            c for c in contracts if c["creator_id"] == inter.author.id
        ]
        if not eligible:
            await inter.response.send_message("У вас нет контрактов, которые можно закрыть.", ephemeral=True)
            return
        await inter.response.send_message(
            "Выберите контракт, который нужно закрыть:", view=CloseContractView(eligible), ephemeral=True
        )


class KickMemberSelect(disnake.ui.StringSelect):
    def __init__(self, contract: dict, guild: disnake.Guild):
        options = []
        for uid in contract.get("members", []):
            if uid == contract["creator_id"]:
                continue  # создателя из контракта кикнуть нельзя — закрыть контракт может только он сам
            member = guild.get_member(uid)
            label = member.display_name if member else f"ID {uid}"
            options.append(disnake.SelectOption(label=label[:100], value=str(uid)))
        if not options:
            options = [disnake.SelectOption(label="Некого исключить", value="none")]
        super().__init__(placeholder="Кого исключить", options=options)
        self.contract_id = contract["id"]

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.edit_message(content="В этом контракте никого нет.", view=None)
            return

        member_id = int(self.values[0])
        data = contracts_store.load()
        contract = _find_contract(data, self.contract_id)
        if contract is None:
            await inter.response.edit_message(content="❌ Контракт не найден, возможно уже закрыт.", view=None)
            return
        if not _can_close(inter.author, contract):
            await inter.response.send_message("❌ Кикать с контракта может только его создатель.", ephemeral=True)
            return
        if member_id not in contract["members"]:
            await inter.response.edit_message(content="❌ Этот участник уже не в контракте.", view=None)
            await _refresh_contracts_panel(inter.guild)
            return

        contract["members"].remove(member_id)
        contracts_store.save(data)

        await inter.response.edit_message(
            content=f"{icon_tag('kick')} <@{member_id}> исключён из контракта **{contract['title']}**.", view=None
        )
        await _refresh_contracts_panel(inter.guild)
        await _log_event(
            inter.guild,
            f"{icon_tag('kick')} Участник исключён из контракта",
            [
                ("Контракт", contract["title"]),
                ("Кого исключили", f"<@{member_id}>"),
                ("Кто исключил", f"<@{inter.author.id}>"),
            ],
        )


class KickMemberView(disnake.ui.View):
    def __init__(self, contract: dict, guild: disnake.Guild):
        super().__init__(timeout=120)
        self.add_item(KickMemberSelect(contract, guild))


class KickContractSelect(disnake.ui.StringSelect):
    def __init__(self, contracts: list[dict]):
        options = [
            disnake.SelectOption(
                label=f"{c['title']} — {len(c['members'])}/{c['max_members']}"[:100],
                value=str(c["id"]),
            )
            for c in contracts[:25]
        ]
        super().__init__(placeholder="Выберите контракт", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        contract_id = int(self.values[0])
        data = contracts_store.load()
        contract = _find_contract(data, contract_id)
        if contract is None:
            await inter.response.edit_message(content="❌ Контракт не найден, возможно уже закрыт.", view=None)
            return
        if not _can_close(inter.author, contract):
            await inter.response.send_message("❌ Кикать с контракта может только его создатель.", ephemeral=True)
            return
        if len(contract.get("members", [])) <= 1:
            await inter.response.edit_message(content="Кроме вас, в контракте пока никого нет.", view=None)
            return

        await inter.response.edit_message(
            content=f"Кого исключить из **{contract['title']}**?", view=KickMemberView(contract, inter.guild)
        )


class KickContractView(disnake.ui.View):
    def __init__(self, contracts: list[dict]):
        super().__init__(timeout=120)
        self.add_item(KickContractSelect(contracts))


class KickMemberButton(disnake.ui.Button):
    def __init__(self):
        super().__init__(label="Кикнуть с контракта", style=disnake.ButtonStyle.secondary, emoji=icon("kick"), custom_id="contracts_kick")

    async def callback(self, inter: disnake.MessageInteraction):
        contracts = contracts_store.load()["contracts"]
        eligible = [
            c for c in contracts
            if len(c.get("members", [])) > 1
            and (c["creator_id"] == inter.author.id or inter.author.guild_permissions.manage_guild)
        ]
        if not eligible:
            await inter.response.send_message("У вас нет контрактов с участниками, которых можно кикнуть.", ephemeral=True)
            return
        await inter.response.send_message("Выберите контракт:", view=KickContractView(eligible), ephemeral=True)


class ContractsPanelView(disnake.ui.View):
    def __init__(self, contracts: list[dict]):
        super().__init__(timeout=None)
        self.add_item(TakeContractSelect(contracts))
        self.add_item(CreateContractButton())
        self.add_item(CloseContractButton())
        self.add_item(KickMemberButton())


def _build_contracts_panel_view() -> ContractsPanelView:
    return ContractsPanelView(contracts_store.load()["contracts"])


# ---------------------------------------------------------------------------
# Слэш-команда
# ---------------------------------------------------------------------------

class Contracts(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self.check_expired_contracts.start()

    def cog_unload(self):
        self.check_expired_contracts.cancel()

    @commands.slash_command(name="contract", description="Показать панель контрактов семьи")
    async def contract(self, inter: disnake.ApplicationCommandInteraction):
        data = contracts_store.load()
        embed = _build_contracts_embed(data)
        message = await send_panel(inter, embed, view=_build_contracts_panel_view(), panel_key="contracts")

        data = contracts_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        contracts_store.save(data)

    @tasks.loop(minutes=EXPIRY_CHECK_MINUTES)
    async def check_expired_contracts(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return

        data = contracts_store.load()
        now = dt.datetime.now(dt.timezone.utc)
        due = [c for c in data["contracts"] if not c.get("expiry_notified") and _timestamp(c.get("expires_at"))]
        due = [c for c in due if now >= dt.datetime.fromisoformat(c["expires_at"])]
        if not due:
            return

        channel_id = data.get("panel_channel_id") or log_channel_id("contracts")
        channel = guild.get_channel(channel_id) if channel_id else None

        for contract in due:
            contract["expiry_notified"] = True
            if channel is not None:
                try:
                    await channel.send(
                        f"⏰ <@{contract['creator_id']}>, время выполнения контракта "
                        f"**{contract['title']}** истекло — закройте его, если он выполнен."
                    )
                except disnake.HTTPException:
                    pass

        contracts_store.save(data)

    @check_expired_contracts.before_loop
    async def _before_check_expired_contracts(self):
        await self.bot.wait_until_ready()


def setup(bot: commands.InteractionBot):
    bot.add_cog(Contracts(bot))


PERSISTENT_VIEWS = [lambda: _build_contracts_panel_view()]
