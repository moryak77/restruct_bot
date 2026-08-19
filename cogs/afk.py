from __future__ import annotations

import datetime as dt

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.icons import icon, icon_tag
from core.storage import afk_store


def _build_afk_embed() -> disnake.Embed:
    entries = afk_store.load()["entries"]
    afk_list = [e for e in entries if e["type"] == "afk"]
    vacation_list = [e for e in entries if e["type"] == "vacation"]

    embed = base_embed(
        "🕓 Сотрудники в AFK и отпуске",
        "Актуальный статус занятости сотрудников семьи. Чтобы изменить свой статус, воспользуйтесь кнопками ниже.",
        panel_key="afk",
    )

    def fmt(entry: dict) -> str:
        line = f"• <@{entry['user_id']}> — {entry['reason']}"
        if entry.get("until"):
            line += f" (до {entry['until']})"
        return line

    embed.add_field(
        name="🌙 В AFK",
        value="\n".join(fmt(e) for e in afk_list) if afk_list else "Никого нет",
        inline=False,
    )
    embed.add_field(
        name="🏖 В отпуске",
        value="\n".join(fmt(e) for e in vacation_list) if vacation_list else "Никого нет",
        inline=False,
    )
    embed.add_field(
        name=f"{icon_tag('settings')} Кнопки",
        value=(
            f"{icon_tag('moon')} — **Уйти в AFK**\n"
            f"{icon_tag('beach')} — **Уйти в отпуск**\n"
            f"{icon_tag('check')} — **Вернуться** (снять статус)"
        ),
        inline=False,
    )
    return embed


def _set_status(user_id: int, status_type: str, reason: str, until: str | None) -> None:
    data = afk_store.load()
    data["entries"] = [e for e in data["entries"] if e["user_id"] != user_id]
    data["entries"].append({
        "user_id": user_id,
        "type": status_type,
        "reason": reason,
        "until": until,
        "since": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    afk_store.save(data)


def _clear_status(user_id: int) -> bool:
    data = afk_store.load()
    before = len(data["entries"])
    data["entries"] = [e for e in data["entries"] if e["user_id"] != user_id]
    afk_store.save(data)
    return len(data["entries"]) < before


class StatusModal(disnake.ui.Modal):
    def __init__(self, status_type: str, title: str):
        self.status_type = status_type
        components = [
            disnake.ui.TextInput(
                label="Причина",
                custom_id="reason",
                style=disnake.TextInputStyle.short,
                max_length=100,
            ),
            disnake.ui.TextInput(
                label="До какого числа (необязательно)",
                custom_id="until",
                style=disnake.TextInputStyle.short,
                max_length=40,
                required=False,
            ),
        ]
        super().__init__(title=title, components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        reason = inter.text_values["reason"].strip()
        until = inter.text_values["until"].strip() or None
        _set_status(inter.author.id, self.status_type, reason, until)

        if inter.message is not None:
            await inter.response.edit_message(embed=_build_afk_embed())
        else:
            await inter.response.send_message("✅ Статус обновлён.", ephemeral=True)


class AfkPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @disnake.ui.button(label="Уйти в AFK", style=disnake.ButtonStyle.secondary, emoji=icon("moon"), custom_id="afk_go")
    async def go_afk(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(StatusModal("afk", "Уйти в AFK"))

    @disnake.ui.button(label="Уйти в отпуск", style=disnake.ButtonStyle.secondary, emoji=icon("beach"), custom_id="afk_vacation")
    async def go_vacation(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(StatusModal("vacation", "Уйти в отпуск"))

    @disnake.ui.button(label="Вернуться", style=disnake.ButtonStyle.secondary, emoji=icon("check"), custom_id="afk_return")
    async def return_button(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        changed = _clear_status(inter.author.id)
        if not changed:
            await inter.response.send_message("У вас нет активного статуса AFK или отпуска.", ephemeral=True)
            return
        await inter.response.edit_message(embed=_build_afk_embed())


class Afk(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        # on_message летит на КАЖДОЕ сообщение сервера — один afk_store.load() на всё
        # (self-clear + все упоминания), а не по load() на каждое упомянутое имя.
        if message.author.bot or message.guild is None:
            return

        data = afk_store.load()
        entries = data["entries"]

        author_entry = next(
            (e for e in entries if e["user_id"] == message.author.id and e["type"] == "afk"), None
        )
        if author_entry is not None:
            entries.remove(author_entry)
            afk_store.save(data)
            try:
                await message.channel.send(
                    f"✅ {message.author.mention}, с возвращением! Статус AFK снят автоматически.",
                    delete_after=10,
                )
            except disnake.HTTPException:
                pass

        if not message.mentions:
            return

        lines = []
        for member in message.mentions:
            if member.bot or member.id == message.author.id:
                continue
            entry = next((e for e in entries if e["user_id"] == member.id), None)
            if entry is not None:
                label = "в AFK" if entry["type"] == "afk" else "в отпуске"
                lines.append(f"⚠️ {member.mention} сейчас {label}: {entry['reason']}")

        if lines:
            try:
                await message.channel.send("\n".join(lines))
            except disnake.HTTPException:
                pass

    @commands.slash_command(name="afk", description="Показать панель сотрудников в AFK и отпуске")
    async def afk(self, inter: disnake.ApplicationCommandInteraction):
        await send_panel(inter, _build_afk_embed(), view=AfkPanelView(), panel_key="afk")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Afk(bot))


PERSISTENT_VIEWS = [lambda: AfkPanelView()]
