from __future__ import annotations

import disnake
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config
from core.storage import voice_store

ADMIN_PERMS = disnake.Permissions(manage_guild=True)

# У войса свой набор кастомных иконок (загружены отдельно, Tabler Icons, тот же серый
# тон #B5BAC1, что и у остального бота, но другая форма/набор — префикс "vcg_") —
# так панель голосовых каналов визуально отличается от остальных панелей своим
# набором значков, оставаясь в общей цветовой гамме.
_VOICE_ICON_IDS = {
    "lock": 1538632042022043679,
    "unlock": 1538632048070098964,
    "hide": 1538632053762035804,
    "reveal": 1538632059965276340,
    "rename": 1538632065782779924,
    "increase": 1538632071885365359,
    "decrease": 1538632077560381560,
    "kick": 1538632083461771284,
    "claim_channel": 1538632089522540575,
    "transfer": 1538632095436509384,
    "invite": 1538632101547610279,
    "delete": 1538632107583213698,
}
VOICE_EMOJI = {key: disnake.PartialEmoji(name=f"vcg_{key}", id=emoji_id) for key, emoji_id in _VOICE_ICON_IDS.items()}
VOICE_EMOJI["settings"] = "⚙️"


def _get_temp_channel(member: disnake.Member) -> tuple[disnake.VoiceChannel | None, dict | None]:
    if member.voice is None or member.voice.channel is None:
        return None, None
    data = voice_store.load()
    info = data["channels"].get(str(member.voice.channel.id))
    if info is None:
        return None, None
    return member.voice.channel, info


async def _prune_empty_voice_channels(guild: disnake.Guild) -> None:
    """Убирает приватные каналы, которые опустели (или были удалены вручную) пока бот
    был выключен — иначе они висят вечно: новых событий на уже пустом канале не будет."""
    data = voice_store.load()
    changed = False
    for channel_id in list(data["channels"].keys()):
        channel = guild.get_channel(int(channel_id))
        if channel is None or len(channel.members) == 0:
            data["channels"].pop(channel_id, None)
            changed = True
            if channel is not None:
                try:
                    await channel.delete(reason="Приватный голосовой канал опустел (проверка при запуске бота)")
                except disnake.HTTPException:
                    pass
    if changed:
        voice_store.save(data)


class RenameModal(disnake.ui.Modal):
    def __init__(self, channel: disnake.VoiceChannel):
        self.channel = channel
        components = [
            disnake.ui.TextInput(
                label="Новое название канала",
                custom_id="name",
                style=disnake.TextInputStyle.short,
                max_length=95,
            ),
        ]
        super().__init__(title="Переименовать канал", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        new_name = inter.text_values["name"].strip()
        await self.channel.edit(name=new_name, reason=f"Переименовано владельцем {inter.author}")
        await inter.response.send_message(f"✅ Канал переименован в **{new_name}**.", ephemeral=True)


class KickSelect(disnake.ui.StringSelect):
    def __init__(self, channel: disnake.VoiceChannel):
        self.channel = channel
        options = [
            disnake.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in channel.members
        ]
        super().__init__(placeholder="Кого отключить от канала", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        member = self.channel.guild.get_member(int(self.values[0]))
        if member is None or member.voice is None or member.voice.channel != self.channel:
            await inter.response.edit_message(content="❌ Участник уже покинул канал.", view=None)
            return
        await member.move_to(None, reason=f"Отключён владельцем канала {inter.author}")
        await inter.response.edit_message(content=f"✅ {member.mention} отключён от канала.", view=None)


class KickView(disnake.ui.View):
    def __init__(self, channel: disnake.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(KickSelect(channel))


class TransferSelect(disnake.ui.StringSelect):
    def __init__(self, channel: disnake.VoiceChannel, current_owner_id: int):
        self.channel = channel
        options = [
            disnake.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in channel.members
            if m.id != current_owner_id
        ]
        super().__init__(placeholder="Кому передать права владельца", options=options)

    async def callback(self, inter: disnake.MessageInteraction):
        new_owner = self.channel.guild.get_member(int(self.values[0]))
        if new_owner is None or new_owner.voice is None or new_owner.voice.channel != self.channel:
            await inter.response.edit_message(content="❌ Участник уже покинул канал.", view=None)
            return

        data = voice_store.load()
        info = data["channels"].get(str(self.channel.id))
        if info is None:
            await inter.response.edit_message(content="❌ Канал больше не отслеживается ботом.", view=None)
            return

        await self.channel.set_permissions(new_owner, manage_channels=True, move_members=True, mute_members=True)
        old_owner_id = info["owner_id"]
        info["owner_id"] = new_owner.id
        voice_store.save(data)

        old_owner = self.channel.guild.get_member(old_owner_id)
        if old_owner is not None:
            await self.channel.set_permissions(old_owner, overwrite=None)

        await inter.response.edit_message(content=f"✅ Права владельца переданы {new_owner.mention}.", view=None)


class TransferView(disnake.ui.View):
    def __init__(self, channel: disnake.VoiceChannel, current_owner_id: int):
        super().__init__(timeout=60)
        self.add_item(TransferSelect(channel, current_owner_id))


class InviteSelect(disnake.ui.UserSelect):
    def __init__(self, channel: disnake.VoiceChannel):
        self.channel = channel
        super().__init__(placeholder="Кого впустить в канал", min_values=1, max_values=1)

    async def callback(self, inter: disnake.MessageInteraction):
        target = self.values[0]
        if not isinstance(target, disnake.Member):
            await inter.response.edit_message(content="❌ Этот пользователь не на сервере.", view=None)
            return
        if target.bot:
            await inter.response.edit_message(content="❌ Ботов приглашать нельзя.", view=None)
            return

        await self.channel.set_permissions(
            target, connect=True, view_channel=True, reason=f"Приглашён владельцем канала {inter.author}"
        )
        await inter.response.edit_message(
            content=f"✅ {target.mention} теперь может зайти в канал, даже если он заблокирован или скрыт.",
            view=None,
        )
        try:
            await self.channel.send(
                f"{target.mention}, {inter.author.mention} открыл(а) вам доступ к этому каналу.",
                delete_after=30,
            )
        except disnake.HTTPException:
            pass


class InviteView(disnake.ui.View):
    def __init__(self, channel: disnake.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(InviteSelect(channel))


def build_voice_help_embed(title: str, description: str | None = None, **base_embed_kwargs) -> disnake.Embed:
    """Embed с легендой кнопок VoiceControlView для панели /voice."""
    embed = base_embed(title, description, **base_embed_kwargs)
    embed.add_field(
        name=f"{VOICE_EMOJI['settings']} Кнопки",
        value=(
            f"{VOICE_EMOJI['lock']} — **Заблокировать** канал\n"
            f"{VOICE_EMOJI['unlock']} — **Разблокировать** канал\n"
            f"{VOICE_EMOJI['hide']} — **Скрыть** канал\n"
            f"{VOICE_EMOJI['reveal']} — **Показать** канал\n"
            f"{VOICE_EMOJI['rename']} — **Переименовать** канал\n"
            f"{VOICE_EMOJI['increase']} — **Увеличить** лимит участников\n"
            f"{VOICE_EMOJI['decrease']} — **Уменьшить** лимит участников\n"
            f"{VOICE_EMOJI['kick']} — **Кикнуть** участника из канала\n"
            f"{VOICE_EMOJI['claim_channel']} — **Забрать** канал (если владелец вышел)\n"
            f"{VOICE_EMOJI['transfer']} — **Передать** права владельца\n"
            f"{VOICE_EMOJI['invite']} — **Пригласить** участника (доступ даже в закрытый/скрытый канал)\n"
            f"{VOICE_EMOJI['delete']} — **Удалить** канал"
        ),
        inline=False,
    )
    return embed


class VoiceControlView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _require_owner(self, inter: disnake.MessageInteraction) -> disnake.VoiceChannel | None:
        channel, info = _get_temp_channel(inter.author)
        if channel is None:
            await inter.response.send_message(
                "❌ Вы должны находиться в своём голосовом канале, созданном через приватную комнату.",
                ephemeral=True,
            )
            return None
        if info["owner_id"] != inter.author.id:
            await inter.response.send_message("❌ Управлять каналом может только его владелец.", ephemeral=True)
            return None
        return channel

    # Ряд 1
    @disnake.ui.button(emoji=VOICE_EMOJI['lock'], style=disnake.ButtonStyle.secondary, custom_id="voice_lock", row=0)
    async def lock(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await channel.set_permissions(inter.guild.default_role, connect=False)
        await inter.response.send_message("🔒 Канал заблокирован — новые участники не смогут зайти без приглашения.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['unlock'], style=disnake.ButtonStyle.secondary, custom_id="voice_unlock", row=0)
    async def unlock(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await channel.set_permissions(inter.guild.default_role, connect=None)
        await inter.response.send_message("🔓 Канал разблокирован — открыт для всех.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['hide'], style=disnake.ButtonStyle.secondary, custom_id="voice_hide", row=0)
    async def hide(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await channel.set_permissions(inter.guild.default_role, view_channel=False)
        await inter.response.send_message("🙈 Канал скрыт — его больше не видно в списке каналов.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['reveal'], style=disnake.ButtonStyle.secondary, custom_id="voice_reveal", row=0)
    async def reveal(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await channel.set_permissions(inter.guild.default_role, view_channel=None)
        await inter.response.send_message("👁️ Канал снова виден всем.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['rename'], style=disnake.ButtonStyle.secondary, custom_id="voice_rename", row=0)
    async def rename(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await inter.response.send_modal(RenameModal(channel))

    # Ряд 2
    @disnake.ui.button(emoji=VOICE_EMOJI['increase'], style=disnake.ButtonStyle.secondary, custom_id="voice_increase", row=1)
    async def increase_limit(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        new_limit = min(99, (channel.user_limit or 0) + 1)
        await channel.edit(user_limit=new_limit, reason=f"Лимит увеличен владельцем {inter.author}")
        await inter.response.send_message(f"➕ Лимит участников увеличен до **{new_limit}**.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['decrease'], style=disnake.ButtonStyle.secondary, custom_id="voice_decrease", row=1)
    async def decrease_limit(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        current = channel.user_limit or 0
        if current == 0:
            await inter.response.send_message("❌ Лимит уже не установлен (без ограничений).", ephemeral=True)
            return
        new_limit = max(0, current - 1)
        await channel.edit(user_limit=new_limit, reason=f"Лимит уменьшен владельцем {inter.author}")
        text = "снят" if new_limit == 0 else f"уменьшен до **{new_limit}**"
        await inter.response.send_message(f"➖ Лимит участников {text}.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['kick'], style=disnake.ButtonStyle.secondary, custom_id="voice_kick", row=1)
    async def kick(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        others = [m for m in channel.members if m.id != inter.author.id]
        if not others:
            await inter.response.send_message("В канале больше никого нет.", ephemeral=True)
            return
        await inter.response.send_message("Кого отключить от канала?", view=KickView(channel), ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['claim_channel'], style=disnake.ButtonStyle.secondary, custom_id="voice_claim", row=1)
    async def claim(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel, info = _get_temp_channel(inter.author)
        if channel is None:
            await inter.response.send_message(
                "❌ Вы должны находиться в приватном голосовом канале, чтобы забрать его.",
                ephemeral=True,
            )
            return
        owner_still_here = inter.guild.get_member(info["owner_id"]) in channel.members
        if owner_still_here:
            await inter.response.send_message("❌ Владелец всё ещё находится в канале.", ephemeral=True)
            return

        data = voice_store.load()
        old_owner_id = info["owner_id"]
        data["channels"][str(channel.id)]["owner_id"] = inter.author.id
        voice_store.save(data)

        await channel.set_permissions(inter.author, manage_channels=True, move_members=True, mute_members=True)
        old_owner = inter.guild.get_member(old_owner_id)
        if old_owner is not None:
            await channel.set_permissions(old_owner, overwrite=None)

        await inter.response.send_message("👑 Вы стали новым владельцем канала.", ephemeral=True)

    @disnake.ui.button(emoji=VOICE_EMOJI['transfer'], style=disnake.ButtonStyle.secondary, custom_id="voice_transfer", row=1)
    async def transfer(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        others = [m for m in channel.members if m.id != inter.author.id]
        if not others:
            await inter.response.send_message("В канале больше никого нет.", ephemeral=True)
            return
        await inter.response.send_message(
            "Кому передать права владельца?", view=TransferView(channel, inter.author.id), ephemeral=True
        )

    @disnake.ui.button(emoji=VOICE_EMOJI['invite'], style=disnake.ButtonStyle.secondary, custom_id="voice_invite", row=2)
    async def invite(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return
        await inter.response.send_message(
            "Кого впустить в канал (даже если он заблокирован или скрыт)?",
            view=InviteView(channel),
            ephemeral=True,
        )

    @disnake.ui.button(emoji=VOICE_EMOJI['delete'], style=disnake.ButtonStyle.secondary, custom_id="voice_delete", row=2)
    async def delete(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        channel = await self._require_owner(inter)
        if channel is None:
            return

        data = voice_store.load()
        data["channels"].pop(str(channel.id), None)
        voice_store.save(data)

        await inter.response.send_message("🗑️ Канал удаляется...", ephemeral=True)
        try:
            await channel.delete(reason=f"Канал удалён владельцем {inter.author}")
        except disnake.HTTPException:
            pass


class Voice(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._creating: set[int] = set()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState
    ):
        trigger_id = config.get("channels.join_to_create")
        category_id = config.get("channels.voice_category")

        if (
            trigger_id
            and after.channel is not None
            and after.channel.id == trigger_id
            and member.id not in self._creating
        ):
            self._creating.add(member.id)
            try:
                guild = member.guild
                category = guild.get_channel(category_id) if category_id else after.channel.category
                try:
                    new_channel = await guild.create_voice_channel(
                        f"🔊 Канал {member.display_name}"[:100],
                        category=category if isinstance(category, disnake.CategoryChannel) else None,
                        overwrites={
                            member: disnake.PermissionOverwrite(
                                manage_channels=True, move_members=True, mute_members=True
                            ),
                        },
                        reason=f"Приватный канал для {member} ({member.id})",
                    )
                except disnake.Forbidden:
                    return

                data = voice_store.load()
                data["channels"][str(new_channel.id)] = {"owner_id": member.id}
                voice_store.save(data)

                try:
                    await member.move_to(new_channel, reason="Перемещение в приватный голосовой канал")
                except disnake.HTTPException:
                    pass
            finally:
                self._creating.discard(member.id)

        if before.channel is not None and before.channel != after.channel:
            data = voice_store.load()
            key = str(before.channel.id)
            if key in data["channels"] and len(before.channel.members) == 0:
                data["channels"].pop(key, None)
                voice_store.save(data)
                try:
                    await before.channel.delete(reason="Приватный голосовой канал опустел")
                except disnake.HTTPException:
                    pass

    @commands.slash_command(
        name="voice",
        description="Создать панель управления голосовыми каналами",
        default_member_permissions=ADMIN_PERMS,
    )
    async def voice(self, inter: disnake.ApplicationCommandInteraction):
        embed = build_voice_help_embed(
            "🔊 Панель управления голосовым каналом",
            "Зайдите в канал создания приватной комнаты, чтобы получить свой личный голосовой канал.\n"
            "Находясь в своём канале, используйте кнопки ниже, чтобы им управлять.",
            panel_key="voice",
        )
        await send_panel(inter, embed, view=VoiceControlView(), panel_key="voice")


def setup(bot: commands.InteractionBot):
    bot.add_cog(Voice(bot))


PERSISTENT_VIEWS = [lambda: VoiceControlView()]
