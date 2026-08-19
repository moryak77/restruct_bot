from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
import time
from dataclasses import dataclass

import aiohttp
import disnake
import yt_dlp
from disnake.ext import commands

from core.branding import base_embed, send_panel
from core.config import config, log_channel_id
from core.http import get_session
from core.storage import music_store

# ---------------------------------------------------------------------------
# Иконки (свой серый набор, тот же тон #B5BAC1, что у войса и остального бота)
# ---------------------------------------------------------------------------

_MUSIC_ICON_IDS = {
    "add": 1538632178773000313,
    "pause": 1538632185165119550,
    "resume": 1538632192245104780,
    "skip": 1538632198960455801,
    "stop": 1538632204974825532,
    "queue": 1538632211526590604,
    "shuffle": 1538632217482367077,
    "loop": 1538632223819956327,
    "vol_up": 1538632229528543372,
    "vol_down": 1538632235421536358,
    "leave": 1538632241419264203,
}
MUSIC_EMOJI = {key: disnake.PartialEmoji(name=f"m_{key}", id=emoji_id) for key, emoji_id in _MUSIC_ICON_IDS.items()}


# ---------------------------------------------------------------------------
# YouTube (через yt-dlp) — реальный источник звука для всех запросов
# ---------------------------------------------------------------------------

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    # На датацентровых/хостинговых IP YouTube чаще отдаёт 403 на клиент по умолчанию
    # (android_vr) — "android" и "web_safari" на практике надёжнее в такой обстановке.
    "extractor_args": {"youtube": {"player_client": ["android", "web_safari"]}},
}

FFMPEG_EXECUTABLE = config.get("music.ffmpeg_path") or "ffmpeg"
FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

MAX_QUEUE = config.get("music.max_queue", 50) or 50
IDLE_DISCONNECT_MINUTES = config.get("music.idle_disconnect_minutes", 5) or 5


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int | None
    thumbnail: str | None
    requester_id: int


def _extract_sync(query: str) -> dict | None:
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        return ydl.extract_info(query, download=False)


async def _extract(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_sync, query)


# ---------------------------------------------------------------------------
# Spotify — только для превращения ссылки на трек в поисковый запрос
# (сам звук Spotify через API не отдаёт, поэтому играем найденный трек с YouTube)
# ---------------------------------------------------------------------------

_SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([a-zA-Z0-9]+)")
_spotify_token: str | None = None
_spotify_token_expires: float = 0.0


def spotify_configured() -> bool:
    return bool(config.get("music.spotify_client_id") and config.get("music.spotify_client_secret"))


async def _spotify_token_get(session: aiohttp.ClientSession) -> str | None:
    global _spotify_token, _spotify_token_expires
    client_id = config.get("music.spotify_client_id")
    client_secret = config.get("music.spotify_client_secret")
    if not client_id or not client_secret:
        return None
    if _spotify_token and time.monotonic() < _spotify_token_expires:
        return _spotify_token

    auth = aiohttp.BasicAuth(client_id, client_secret)
    async with session.post(
        "https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"}, auth=auth
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()

    _spotify_token = data["access_token"]
    _spotify_token_expires = time.monotonic() + data.get("expires_in", 3600) - 60
    return _spotify_token


async def _spotify_query_for(url: str) -> str | None:
    match = _SPOTIFY_TRACK_RE.search(url)
    if not match:
        return None
    track_id = match.group(1)

    session = get_session()
    token = await _spotify_token_get(session)
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=headers) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()

    artists = ", ".join(a["name"] for a in data.get("artists", []))
    title = data.get("name", "")
    return f"{artists} - {title}".strip(" -") or None


async def resolve_track(query: str, requester_id: int) -> Track | None:
    search_query = query.strip()

    if "open.spotify.com" in search_query:
        resolved = await _spotify_query_for(search_query)
        if not resolved:
            return None
        search_query = resolved

    try:
        info = await _extract(search_query)
    except Exception:
        return None

    if info is None:
        return None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            return None
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url:
        return None

    return Track(
        title=info.get("title") or "Без названия",
        webpage_url=info.get("webpage_url") or search_query,
        stream_url=stream_url,
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        requester_id=requester_id,
    )


# ---------------------------------------------------------------------------
# Состояние плеера по серверу
# ---------------------------------------------------------------------------

class GuildMusicState:
    def __init__(self) -> None:
        self.voice_client: disnake.VoiceClient | None = None
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.started_at: dt.datetime | None = None
        self.loop = False
        self.volume = 1.0
        self.idle_task: asyncio.Task | None = None
        self.owner_id: int | None = None


GUILD_STATES: dict[int, GuildMusicState] = {}


def _state(guild_id: int) -> GuildMusicState:
    if guild_id not in GUILD_STATES:
        GUILD_STATES[guild_id] = GuildMusicState()
    return GUILD_STATES[guild_id]


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _log_event(guild: disnake.Guild, title: str, fields: list[tuple[str, str]]) -> None:
    channel_id = log_channel_id("music")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    embed = base_embed(title, color=disnake.Color.dark_grey())
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=True)
    await channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Панель
# ---------------------------------------------------------------------------

_BUTTONS_HELP = (
    f"{MUSIC_EMOJI['add']} — **Добавить трек** — ссылка (YouTube/Spotify) или название\n"
    f"{MUSIC_EMOJI['pause']} — **Пауза** / {MUSIC_EMOJI['resume']} **Продолжить**\n"
    f"{MUSIC_EMOJI['skip']} — **Пропустить** текущий трек\n"
    f"{MUSIC_EMOJI['stop']} — **Стоп** — остановить и очистить очередь\n"
    f"{MUSIC_EMOJI['queue']} — **Показать очередь** (видно только вам)\n"
    f"{MUSIC_EMOJI['shuffle']} — **Перемешать** очередь\n"
    f"{MUSIC_EMOJI['loop']} — **Повтор** текущего трека\n"
    f"{MUSIC_EMOJI['vol_down']} / {MUSIC_EMOJI['vol_up']} — **Тише** / **Громче**\n"
    f"{MUSIC_EMOJI['leave']} — **Выйти** из голосового канала"
)


def _build_music_embed(guild: disnake.Guild) -> disnake.Embed:
    state = GUILD_STATES.get(guild.id)
    embed = base_embed("🎵 Музыкальная панель", panel_key="music")

    if state is None or (state.current is None and not state.queue):
        embed.description = f"Сейчас ничего не играет. Нажмите {MUSIC_EMOJI['add']}, чтобы начать."
        embed.add_field(name=f"{MUSIC_EMOJI['add']} Кнопки", value=_BUTTONS_HELP, inline=False)
        embed.add_field(
            name="ℹ️ Как это работает",
            value=(
                "Первый, кто включит трек, становится владельцем сессии — пока он слушает "
                "(не вышел из голосового канала), управлять может только он. "
                "Как только он выйдет — плеер снова свободен."
            ),
            inline=False,
        )
        return embed

    if state.current is not None:
        ts = int(state.started_at.timestamp()) if state.started_at else None
        when = f" · играет с <t:{ts}:R>" if ts else ""
        embed.add_field(
            name=f"{MUSIC_EMOJI['resume']} Сейчас играет",
            value=(
                f"[{state.current.title}]({state.current.webpage_url})\n"
                f"⏱️ {_fmt_duration(state.current.duration)} · запросил <@{state.current.requester_id}>{when}"
            ),
            inline=False,
        )
        if state.current.thumbnail:
            embed.set_thumbnail(url=state.current.thumbnail)
    else:
        embed.add_field(name=f"{MUSIC_EMOJI['resume']} Сейчас играет", value="Ничего.", inline=False)

    if state.queue:
        lines = [f"`{i + 1}.` {t.title} — <@{t.requester_id}>" for i, t in enumerate(state.queue[:10])]
        extra = f"\n*...и ещё {len(state.queue) - 10}*" if len(state.queue) > 10 else ""
        embed.add_field(
            name=f"{MUSIC_EMOJI['queue']} Очередь ({len(state.queue)})",
            value=("\n".join(lines) + extra)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name=f"{MUSIC_EMOJI['queue']} Очередь", value="Пусто.", inline=False)

    embed.add_field(name=f"{MUSIC_EMOJI['loop']} Повтор", value="Включён" if state.loop else "Выключен", inline=True)
    embed.add_field(name=f"{MUSIC_EMOJI['vol_up']} Громкость", value=f"{int(state.volume * 100)}%", inline=True)
    embed.add_field(
        name="👤 Управляет",
        value=f"<@{state.owner_id}> — выйдет из канала, освободит плеер." if state.owner_id else "Свободно.",
        inline=True,
    )
    embed.add_field(name=f"{MUSIC_EMOJI['add']} Кнопки", value=_BUTTONS_HELP, inline=False)
    return embed


async def _refresh_music_panel(guild: disnake.Guild) -> None:
    data = music_store.load()
    channel_id = data.get("panel_channel_id")
    message_id = data.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    embed = _build_music_embed(guild)
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=MusicPanelView())
    except disnake.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Воспроизведение
# ---------------------------------------------------------------------------

def _schedule_idle_disconnect(guild: disnake.Guild, state: GuildMusicState) -> None:
    if state.idle_task and not state.idle_task.done():
        return
    state.idle_task = asyncio.create_task(_idle_disconnect(guild, state))


async def _idle_disconnect(guild: disnake.Guild, state: GuildMusicState) -> None:
    await asyncio.sleep(IDLE_DISCONNECT_MINUTES * 60)
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return
    channel_empty = not any(not m.bot for m in vc.channel.members)
    nothing_to_play = state.current is None and not state.queue
    if channel_empty or nothing_to_play:
        await vc.disconnect(force=True)
        state.voice_client = None
        state.queue.clear()
        state.current = None
        state.owner_id = None
        await _refresh_music_panel(guild)


def _make_source(track: Track, volume: float) -> disnake.PCMVolumeTransformer:
    """Спавнит процесс ffmpeg — на Windows это иногда занимает больше 3 секунд (например,
    антивирус сканирует exe), поэтому вызывать нужно только из executor'а, не из event loop."""
    return disnake.PCMVolumeTransformer(
        disnake.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG_EXECUTABLE,
            before_options=FFMPEG_BEFORE_OPTS,
            options=FFMPEG_OPTS,
        ),
        volume=volume,
    )


async def _begin_playback(guild: disnake.Guild, state: GuildMusicState, track: Track, loop: asyncio.AbstractEventLoop) -> None:
    source = await loop.run_in_executor(None, _make_source, track, state.volume)
    state.current = track
    state.started_at = dt.datetime.now(dt.timezone.utc)

    def after(error: Exception | None) -> None:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_advance(guild, state, loop)))

    state.voice_client.play(source, after=after)


async def _advance(guild: disnake.Guild, state: GuildMusicState, loop: asyncio.AbstractEventLoop) -> None:
    if state.loop and state.current is not None:
        state.queue.insert(0, state.current)
    state.current = None
    state.started_at = None

    if state.voice_client is None or not state.voice_client.is_connected():
        return

    if state.queue:
        next_track = state.queue.pop(0)
        await _begin_playback(guild, state, next_track, loop)
    else:
        _schedule_idle_disconnect(guild, state)

    await _refresh_music_panel(guild)


async def _start_or_queue(guild: disnake.Guild, state: GuildMusicState, track: Track) -> bool:
    """True — трек начал играть сразу, False — встал в очередь."""
    vc = state.voice_client
    if vc is not None and (vc.is_playing() or vc.is_paused()):
        state.queue.append(track)
        return False
    loop = asyncio.get_running_loop()
    await _begin_playback(guild, state, track, loop)
    return True


async def _require_listener(inter: disnake.MessageInteraction, state: GuildMusicState) -> bool:
    vc = inter.guild.voice_client
    if vc is None or not vc.is_connected():
        await inter.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)
        return False
    if inter.author.voice is None or inter.author.voice.channel is None or inter.author.voice.channel.id != vc.channel.id:
        await inter.response.send_message(f"❌ Зайдите в {vc.channel.mention}, чтобы управлять музыкой.", ephemeral=True)
        return False
    return True


async def _require_owner(inter: disnake.MessageInteraction, state: GuildMusicState) -> bool:
    if not await _require_listener(inter, state):
        return False
    if state.owner_id is not None and state.owner_id != inter.author.id and not inter.author.guild_permissions.manage_guild:
        await inter.response.send_message(
            f"❌ Сейчас музыкой управляет <@{state.owner_id}> — дождитесь, пока он выйдет из голосового канала.",
            ephemeral=True,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Добавление трека
# ---------------------------------------------------------------------------

class AddTrackModal(disnake.ui.Modal):
    def __init__(self):
        components = [
            disnake.ui.TextInput(
                label="Ссылка или название трека",
                custom_id="query",
                style=disnake.TextInputStyle.short,
                max_length=200,
                placeholder="Например: never gonna give you up, либо ссылка YouTube/Spotify",
            ),
        ]
        super().__init__(title="Добавить трек", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        query = inter.text_values["query"].strip()
        await inter.response.defer(ephemeral=True)

        if "open.spotify.com" in query and not spotify_configured():
            await inter.followup.send(
                "❌ Ссылки Spotify пока не подключены (нужны Client ID/Secret в конфиге). "
                "Пришлите название трека текстом или ссылку YouTube.",
                ephemeral=True,
            )
            return

        member_voice = inter.author.voice
        if member_voice is None or member_voice.channel is None:
            await inter.followup.send("❌ Зайдите в голосовой канал, чтобы добавить трек.", ephemeral=True)
            return

        vc = inter.guild.voice_client
        if vc is not None and vc.is_connected() and vc.channel.id != member_voice.channel.id:
            await inter.followup.send(f"❌ Бот уже занят в {vc.channel.mention}.", ephemeral=True)
            return

        state = _state(inter.guild_id)
        if (
            state.owner_id is not None
            and state.owner_id != inter.author.id
            and not inter.author.guild_permissions.manage_guild
        ):
            await inter.followup.send(
                f"❌ Сейчас музыкой управляет <@{state.owner_id}> — дождитесь, пока он выйдет из голосового канала.",
                ephemeral=True,
            )
            return
        if len(state.queue) >= MAX_QUEUE:
            await inter.followup.send("❌ Очередь переполнена, попробуйте чуть позже.", ephemeral=True)
            return

        track = await resolve_track(query, inter.author.id)
        if track is None:
            await inter.followup.send("❌ Ничего не нашёл по этому запросу.", ephemeral=True)
            return

        if vc is None or not vc.is_connected():
            try:
                vc = await member_voice.channel.connect()
            except (asyncio.TimeoutError, disnake.ClientException) as e:
                await inter.followup.send(f"❌ Не удалось подключиться к каналу: {e}", ephemeral=True)
                return
        state.voice_client = vc
        if state.owner_id is None:
            state.owner_id = inter.author.id

        started = await _start_or_queue(inter.guild, state, track)
        await inter.followup.send(
            f"{'▶️ Играет' if started else '➕ Добавлено в очередь'}: **{track.title}**", ephemeral=True
        )
        await _log_event(
            inter.guild,
            "🎵 Трек добавлен",
            [
                ("Трек", track.title),
                ("Запросил", f"<@{inter.author.id}>"),
                ("Статус", "Играет сейчас" if started else "В очереди"),
            ],
        )
        await _refresh_music_panel(inter.guild)


# ---------------------------------------------------------------------------
# Панель управления
# ---------------------------------------------------------------------------

class MusicPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # Ряд 1
    @disnake.ui.button(emoji=MUSIC_EMOJI["add"], style=disnake.ButtonStyle.secondary, custom_id="music_add", row=0)
    async def add_track(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        await inter.response.send_modal(AddTrackModal())

    @disnake.ui.button(emoji=MUSIC_EMOJI["pause"], style=disnake.ButtonStyle.secondary, custom_id="music_pause", row=0)
    async def pause_resume(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        vc = inter.guild.voice_client
        if vc.is_playing():
            vc.pause()
            await inter.response.send_message(f"{MUSIC_EMOJI['pause']} Пауза.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await inter.response.send_message(f"{MUSIC_EMOJI['resume']} Продолжаю.", ephemeral=True)
        else:
            await inter.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)
            return
        await _refresh_music_panel(inter.guild)

    @disnake.ui.button(emoji=MUSIC_EMOJI["skip"], style=disnake.ButtonStyle.secondary, custom_id="music_skip", row=0)
    async def skip(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        vc = inter.guild.voice_client
        if not vc.is_playing() and not vc.is_paused():
            await inter.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)
            return
        skipped = state.current
        vc.stop()
        await inter.response.send_message(
            f"{MUSIC_EMOJI['skip']} Пропустил: **{skipped.title if skipped else '...'}**", ephemeral=True
        )

    @disnake.ui.button(emoji=MUSIC_EMOJI["stop"], style=disnake.ButtonStyle.secondary, custom_id="music_stop", row=0)
    async def stop(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        state.queue.clear()
        state.loop = False
        inter.guild.voice_client.stop()
        await inter.response.send_message(f"{MUSIC_EMOJI['stop']} Остановлено, очередь очищена.", ephemeral=True)

    @disnake.ui.button(emoji=MUSIC_EMOJI["queue"], style=disnake.ButtonStyle.secondary, custom_id="music_queue", row=0)
    async def show_queue(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = GUILD_STATES.get(inter.guild_id)
        embed = base_embed(f"{MUSIC_EMOJI['queue']} Очередь")
        if state is None or (not state.queue and state.current is None):
            embed.description = "Очередь пуста."
        else:
            if state.current:
                embed.add_field(
                    name="Сейчас играет", value=f"[{state.current.title}]({state.current.webpage_url})", inline=False
                )
            if state.queue:
                lines = [f"`{i + 1}.` {t.title} — <@{t.requester_id}>" for i, t in enumerate(state.queue[:25])]
                embed.add_field(name=f"Далее ({len(state.queue)})", value="\n".join(lines)[:1024], inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)

    # Ряд 2
    @disnake.ui.button(emoji=MUSIC_EMOJI["shuffle"], style=disnake.ButtonStyle.secondary, custom_id="music_shuffle", row=1)
    async def shuffle(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        if len(state.queue) < 2:
            await inter.response.send_message("❌ В очереди меньше двух треков — нечего перемешивать.", ephemeral=True)
            return
        random.shuffle(state.queue)
        await inter.response.send_message(f"{MUSIC_EMOJI['shuffle']} Очередь перемешана.", ephemeral=True)
        await _refresh_music_panel(inter.guild)

    @disnake.ui.button(emoji=MUSIC_EMOJI["loop"], style=disnake.ButtonStyle.secondary, custom_id="music_loop", row=1)
    async def toggle_loop(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        state.loop = not state.loop
        await inter.response.send_message(
            f"{MUSIC_EMOJI['loop']} Повтор {'включён' if state.loop else 'выключен'}.", ephemeral=True
        )
        await _refresh_music_panel(inter.guild)

    @disnake.ui.button(emoji=MUSIC_EMOJI["vol_down"], style=disnake.ButtonStyle.secondary, custom_id="music_vol_down", row=1)
    async def vol_down(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        state.volume = max(0.0, round(state.volume - 0.1, 2))
        vc = inter.guild.voice_client
        if isinstance(vc.source, disnake.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await inter.response.send_message(f"{MUSIC_EMOJI['vol_down']} Громкость: {int(state.volume * 100)}%.", ephemeral=True)
        await _refresh_music_panel(inter.guild)

    @disnake.ui.button(emoji=MUSIC_EMOJI["vol_up"], style=disnake.ButtonStyle.secondary, custom_id="music_vol_up", row=1)
    async def vol_up(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        state.volume = min(2.0, round(state.volume + 0.1, 2))
        vc = inter.guild.voice_client
        if isinstance(vc.source, disnake.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await inter.response.send_message(f"{MUSIC_EMOJI['vol_up']} Громкость: {int(state.volume * 100)}%.", ephemeral=True)
        await _refresh_music_panel(inter.guild)

    @disnake.ui.button(emoji=MUSIC_EMOJI["leave"], style=disnake.ButtonStyle.secondary, custom_id="music_leave", row=1)
    async def leave(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        state = _state(inter.guild_id)
        if not await _require_owner(inter, state):
            return
        vc = inter.guild.voice_client
        state.queue.clear()
        state.current = None
        state.loop = False
        await vc.disconnect(force=True)
        state.voice_client = None
        state.owner_id = None
        await inter.response.send_message(f"{MUSIC_EMOJI['leave']} Вышел из голосового канала.", ephemeral=True)
        await _refresh_music_panel(inter.guild)


# ---------------------------------------------------------------------------
# Слэш-команда и авто-выход из пустого канала
# ---------------------------------------------------------------------------

class Music(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot

    @commands.slash_command(name="music", description="Показать музыкальную панель")
    async def music(self, inter: disnake.ApplicationCommandInteraction):
        embed = _build_music_embed(inter.guild)
        message = await send_panel(inter, embed, view=MusicPanelView(), panel_key="music")

        data = music_store.load()
        data["panel_channel_id"] = message.channel.id
        data["panel_message_id"] = message.id
        music_store.save(data)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState
    ):
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or before.channel != vc.channel or after.channel == vc.channel:
            return

        state = _state(member.guild.id)
        if state.owner_id == member.id:
            state.owner_id = None
            await _refresh_music_panel(member.guild)

        if not any(not m.bot for m in vc.channel.members):
            _schedule_idle_disconnect(member.guild, state)


def setup(bot: commands.InteractionBot):
    bot.add_cog(Music(bot))


PERSISTENT_VIEWS = [lambda: MusicPanelView()]
