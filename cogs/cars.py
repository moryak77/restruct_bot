from __future__ import annotations

import datetime as dt

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import log_channel_id
from core.icons import icon, icon_tag
from core.storage import cars_store

CHUNK_SIZE = 10


def _sorted_cars() -> list[dict]:
    return sorted(cars_store.load()["cars"], key=lambda c: c["name"].lower())


def _timestamp(iso_value: str | None) -> int | None:
    if not iso_value:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso_value).timestamp())
    except ValueError:
        return None


def _build_cars_embed(cars: list[dict]) -> disnake.Embed:
    embed = base_embed(
        f"{icon_tag('car')} Автопарк семьи",
        "Общий гараж семьи. Берите машину перед использованием и обязательно сдавайте "
        "после — так остальные всегда видят, что сейчас свободно.",
        panel_key="cars",
    )
    embed.add_field(
        name=f"{icon_tag('help')} Как это работает",
        value=(
            f"{icon_tag('key')} — выберите машину в меню **«Взять машину»**, чтобы забрать её из гаража.\n"
            f"{icon_tag('car')} — закончили пользоваться — выберите её в меню **«Сдать машину»**.\n"
            "Сдать машину может тот, кто её взял, либо руководство."
        ),
        inline=False,
    )

    if not cars:
        embed.add_field(name=f"{icon_tag('clipboard')} Машины", value="Автопарк пока пуст.", inline=False)
        return embed

    for i, start in enumerate(range(0, len(cars), CHUNK_SIZE)):
        chunk = cars[start:start + CHUNK_SIZE]
        lines = []
        for car in chunk:
            if car.get("status") == "taken" and car.get("taken_by"):
                ts = _timestamp(car.get("taken_at"))
                when = f"взята <t:{ts}:t> · <t:{ts}:R>" if ts else "взята недавно"
                status = f"🔴 Занята — <@{car['taken_by']}> · {when}"
            else:
                status = "🟢 Свободна"
            lines.append(f"**{car['name']}** — `{car['plate']}`\n┗ {status}")
        name = f"{icon_tag('clipboard')} Машины ({len(cars)})" if i == 0 else f"{icon_tag('clipboard')} Машины (продолжение)"
        embed.add_field(name=name, value="\n\n".join(lines)[:1024], inline=False)

    return embed


async def _refresh_cars_panel(guild: disnake.Guild) -> None:
    data = cars_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    cars = sorted(data["cars"], key=lambda c: c["name"].lower())
    embed = _build_cars_embed(cars)
    view = CarsPanelView(cars)
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=view)
    except disnake.HTTPException:
        pass


async def _log_take(guild: disnake.Guild, car: dict, taker: disnake.Member) -> None:
    channel_id = log_channel_id("cars")
    if not channel_id:
        return
    log_channel = guild.get_channel(channel_id)
    if log_channel is None:
        return

    embed = base_embed(f"{icon_tag('key')} Машина взята", color=disnake.Color.dark_grey())
    embed.add_field(name="Машина", value=f"{car['name']} — `{car['plate']}`", inline=True)
    embed.add_field(name="Взял", value=taker.mention, inline=True)
    await log_channel.send(embed=embed)


async def _log_return(
    guild: disnake.Guild,
    car: dict,
    returner: disnake.Member,
    taken_by: int | None,
) -> None:
    channel_id = log_channel_id("cars")
    if not channel_id:
        return
    log_channel = guild.get_channel(channel_id)
    if log_channel is None:
        return

    embed = base_embed(f"{icon_tag('car')} Машина сдана", color=disnake.Color.dark_grey())
    embed.add_field(name="Машина", value=f"{car['name']} — `{car['plate']}`", inline=True)
    embed.add_field(name="Сдал", value=returner.mention, inline=True)
    if taken_by and taken_by != returner.id:
        embed.add_field(name="Изначально взял", value=f"<@{taken_by}>", inline=True)

    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    embed.add_field(name="Использовалась до", value=f"<t:{now_ts}:t>", inline=True)

    await log_channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Взять / сдать
# ---------------------------------------------------------------------------

class TakeCarSelect(disnake.ui.StringSelect):
    def __init__(self, cars: list[dict]):
        available = [c for c in cars if c.get("status") != "taken"]
        if available:
            options = [
                disnake.SelectOption(label=f"{c['name']} — {c['plate']}"[:100], value=str(c["id"]))
                for c in available[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет свободных машин", value="none")]
        super().__init__(
            placeholder="🔑 Взять машину",
            options=options,
            custom_id="cars_take_select",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет свободных машин.", ephemeral=True)
            return

        car_id = int(self.values[0])
        data = cars_store.load()
        car = next((c for c in data["cars"] if c["id"] == car_id), None)
        if car is None:
            await inter.response.send_message("❌ Эта машина больше не существует.", ephemeral=True)
            await _refresh_cars_panel(inter.guild)
            return
        if car.get("status") == "taken":
            await inter.response.send_message(f"❌ Эту машину уже взял <@{car['taken_by']}>.", ephemeral=True)
            await _refresh_cars_panel(inter.guild)
            return

        car["status"] = "taken"
        car["taken_by"] = inter.author.id
        car["taken_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        cars_store.save(data)

        await inter.response.send_message(f"🔑 Вы взяли **{car['name']}** ({car['plate']}).", ephemeral=True)
        await _refresh_cars_panel(inter.guild)
        await _log_take(inter.guild, car, inter.author)


class ReturnCarSelect(disnake.ui.StringSelect):
    def __init__(self, cars: list[dict]):
        taken = [c for c in cars if c.get("status") == "taken"]
        if taken:
            options = [
                disnake.SelectOption(
                    label=f"{c['name']} — {c['plate']}"[:100],
                    value=str(c["id"]),
                    description=f"Взял ID: {c.get('taken_by')}"[:100],
                )
                for c in taken[:25]
            ]
        else:
            options = [disnake.SelectOption(label="Нет занятых машин", value="none")]
        super().__init__(
            placeholder="🚗 Сдать машину",
            options=options,
            custom_id="cars_return_select",
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if self.values[0] == "none":
            await inter.response.send_message("Сейчас нет занятых машин.", ephemeral=True)
            return

        car_id = int(self.values[0])
        data = cars_store.load()
        car = next((c for c in data["cars"] if c["id"] == car_id), None)
        if car is None:
            await inter.response.send_message("❌ Эта машина больше не существует.", ephemeral=True)
            await _refresh_cars_panel(inter.guild)
            return
        if car.get("status") != "taken":
            await inter.response.send_message("❌ Эта машина уже свободна.", ephemeral=True)
            await _refresh_cars_panel(inter.guild)
            return

        taken_by = car.get("taken_by")
        if taken_by != inter.author.id and not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message(
                f"❌ Эту машину взял <@{taken_by}> — сдать её может только он или руководство.", ephemeral=True
            )
            return

        car["status"] = "available"
        car["taken_by"] = None
        car["taken_at"] = None
        cars_store.save(data)

        await inter.response.send_message(f"🚗 Вы сдали **{car['name']}** ({car['plate']}).", ephemeral=True)
        await _refresh_cars_panel(inter.guild)
        await _log_return(inter.guild, car, inter.author, taken_by)


class CarsPanelView(disnake.ui.View):
    def __init__(self, cars: list[dict]):
        super().__init__(timeout=None)
        self.add_item(TakeCarSelect(cars))
        self.add_item(ReturnCarSelect(cars))


def _build_cars_panel_view() -> CarsPanelView:
    return CarsPanelView(_sorted_cars())


# ---------------------------------------------------------------------------
# Администрирование: добавление / удаление машин из парка
# ---------------------------------------------------------------------------

class AddCarModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Марка и модель",
                custom_id="name",
                placeholder="Например: BMW M5 F90",
                style=disnake.TextInputStyle.short,
                max_length=80,
            ),
            disnake.ui.TextInput(
                label="Гос. номер",
                custom_id="plate",
                placeholder="Например: А123ВС777",
                style=disnake.TextInputStyle.short,
                max_length=20,
            ),
        ]
        super().__init__(title="Добавление автомобиля", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)

        name = inter.text_values["name"].strip()
        plate = inter.text_values["plate"].strip().upper()

        data = cars_store.load()
        car = {
            "id": data["next_id"],
            "name": name,
            "plate": plate,
            "status": "available",
            "taken_by": None,
            "taken_at": None,
            "added_by": inter.author.id,
            "added_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        data["cars"].append(car)
        data["next_id"] += 1
        cars_store.save(data)

        await inter.followup.send(f"✅ Автомобиль **{name}** ({plate}) добавлен в автопарк.", ephemeral=True)
        await _refresh_cars_panel(inter.guild)


class RemoveCarSelect(disnake.ui.StringSelect):
    def __init__(self, cars: list[dict]):
        options = [
            disnake.SelectOption(label=f"{car['name']} — {car['plate']}"[:100], value=str(car["id"]))
            for car in cars[:25]
        ]
        super().__init__(placeholder="Выберите автомобиль для удаления", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        car_id = int(self.values[0])
        data = cars_store.load()
        before = len(data["cars"])
        removed = next((c for c in data["cars"] if c["id"] == car_id), None)
        data["cars"] = [c for c in data["cars"] if c["id"] != car_id]
        cars_store.save(data)

        if removed and len(data["cars"]) < before:
            await inter.response.edit_message(
                content=f"{icon_tag('delete')} Автомобиль **{removed['name']}** ({removed['plate']}) удалён из автопарка.",
                view=None,
            )
            await _refresh_cars_panel(inter.guild)
        else:
            await inter.response.edit_message(content="❌ Автомобиль не найден, возможно уже удалён.", view=None)


class RemoveCarView(disnake.ui.View):
    def __init__(self, cars: list[dict]):
        super().__init__(timeout=120)
        self.add_item(RemoveCarSelect(cars))


class CarsAdminView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Добавить машину", style=disnake.ButtonStyle.secondary, emoji=icon("increase"), custom_id="cars_add")
    async def add_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(AddCarModal())

    @disnake.ui.button(label="Удалить машину", style=disnake.ButtonStyle.secondary, emoji=icon("delete"), custom_id="cars_remove")
    async def remove_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        cars = cars_store.load()["cars"]
        if not cars:
            await inter.response.send_message("Автопарк пуст — удалять нечего.", ephemeral=True)
            return
        await inter.response.send_message(
            "Выберите автомобиль, который нужно удалить:",
            view=RemoveCarView(cars),
            ephemeral=True,
        )


class Cars(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="cars", description="Автопарк семьи")
    async def cars(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @cars.sub_command(name="show", description="Показать панель автопарка")
    async def cars_show(self, inter: disnake.ApplicationCommandInteraction):
        cars = _sorted_cars()
        embed = _build_cars_embed(cars)
        message = await send_panel(inter, embed, view=CarsPanelView(cars), panel_key="cars")

        data = cars_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        cars_store.save(data)

    @cars.sub_command(
        name="adm",
        description="Управление автопарком: добавление и удаление машин (только для персонала)",
    )
    async def cars_adm(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.manage_guild:
            await inter.response.send_message("❌ У вас недостаточно прав для управления автопарком.", ephemeral=True)
            return

        embed = base_embed(
            f"{icon_tag('car')} Управление автопарком",
            "Добавляйте и убирайте машины из общего гаража семьи. Взятие и сдачу машин "
            "участники семьи делают сами через панель `/cars show`.",
        )
        embed.add_field(
            name=f"{icon_tag('settings')} Кнопки",
            value=f"{icon_tag('increase')} — **Добавить** машину\n{icon_tag('delete')} — **Удалить** машину",
            inline=False,
        )
        await inter.response.send_message(embed=embed, view=CarsAdminView(), ephemeral=True)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Cars(bot))


PERSISTENT_VIEWS = [
    lambda: CarsAdminView(),
    lambda: _build_cars_panel_view(),
]
