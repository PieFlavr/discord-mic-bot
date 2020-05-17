# discord-mic-bot -- Discord bot to connect to your microphone
# Copyright (C) 2020  Star Brilliant
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations
import asyncio
import asyncio.queues
import ctypes
import typing
import discord  # type: ignore
import discord.gateway  # type: ignore
import numpy  # type: ignore
import sounddevice  # type: ignore
if typing.TYPE_CHECKING:
    from . import view


class SoundDevice:
    def __init__(self, name: str, is_default: bool) -> None:
        self.name = name
        self.is_default = is_default

    def __repr__(self) -> str:
        if self.is_default:
            return '* {}'.format(self.name)
        return '  {}'.format(self.name)


class Model:
    def __init__(self, discord_bot_token: str) -> None:
        self.v: typing.Optional[view.View] = None
        self.running = True

        self.loop = asyncio.get_running_loop()

        self.discord_bot_token = discord_bot_token
        self.discord_client: discord.Client = discord.Client(max_messages=None, assume_unsync_clock=True)
        self.login_status = 'Starting up…'
        self.guilds: typing.List[discord.Guild] = []
        self.current_viewing_guild: typing.Optional[discord.Guild] = None
        self.channels: typing.List[discord.VoiceChannel] = []
        self.joined: typing.List[discord.VoiceChannel] = []

        self.input_stream: typing.Optional[sounddevice.RawInputStream] = None
        self.audio_queue = asyncio.Queue(1)
        self.muted = False
        self.opus_encoder = discord.opus.Encoder()
        # Use the private function just to satisfy my paranoid of 1 Kbps == 1000 bps.
        # getattr is used to bypass the linter
        self.opus_encoder_private = getattr(discord.opus, '_lib')
        self.opus_encoder_private.opus_encoder_ctl(getattr(self.opus_encoder, '_state'), discord.opus.CTL_SET_BITRATE, 128000)
        # FEC only works for voice, not music, and from my experience it hurts sound quality severely.
        # If you have a high packet loss rate, it is even better to cut the bitrate in half (so you don't burden the server), and duplicate the "socket.sendto" call.
        self.opus_encoder.set_fec(False)
        self.opus_encoder.set_expected_packet_loss_percent(0)

    def __del__(self) -> None:
        if self.input_stream is not None:
            self.input_stream.stop()
            self.input_stream.close()
        asyncio.run(self.discord_client.close())

    def attach_view(self, v: view.View) -> None:
        self.v = v
        self.v.login_status_updated()
        self.v.guilds_updated()
        self.v.device_updated()

    def get_login_status(self) -> str:
        return self.login_status

    def list_guilds(self) -> typing.List[discord.Guild]:
        return self.guilds

    def list_channels(self) -> typing.List[discord.VoiceChannel]:
        return self.channels

    def list_joined(self) -> typing.List[discord.VoiceChannel]:
        return self.joined

    def list_sound_hostapis(self) -> typing.List[str]:
        hostapis = typing.cast(typing.Tuple[typing.Dict[str, typing.Any]], sounddevice.query_hostapis())
        return [i['name'] for i in hostapis]

    def list_sound_input_devices(self, hostapi: str) -> typing.List[SoundDevice]:
        hostapis = typing.cast(typing.Tuple[typing.Dict[str, typing.Any]], sounddevice.query_hostapis())
        devices = typing.cast(sounddevice.DeviceList, sounddevice.query_devices())

        default_input_id, _ = typing.cast(typing.Tuple[typing.Optional[int], typing.Optional[int]], sounddevice.default.device)
        for api in hostapis:
            if api['name'] == hostapi:
                default_input_id = api['default_input_device']
                break
        else:
            return []

        return [SoundDevice(typing.cast(str, dev['name']), idx == default_input_id) for idx, dev in enumerate(typing.cast(typing.Iterable[typing.Dict[str, typing.Any]], devices)) if dev['max_input_channels'] > 0 and dev['hostapi'] < len(hostapis) and hostapis[typing.cast(int, dev['hostapi'])]['name'] == hostapi]

    async def view_guild(self, guild: typing.Optional[discord.Guild]) -> None:
        self.channels = []
        self.current_viewing_guild = guild
        if guild is None:
            return
        channels = await typing.cast(typing.Awaitable[typing.List[discord.abc.GuildChannel]], guild.fetch_channels())
        if self.current_viewing_guild != guild:
            return
        self.channels = [i for i in channels if isinstance(i, discord.VoiceChannel)]
        if self.v is not None:
            self.v.channels_updated()

    async def join_voice(self, channel: discord.VoiceChannel) -> None:
        if channel in self.joined:
            return
        self.joined.append(channel)

        try:
            await channel.connect()
        except Exception as e:
            print(e)
            self.joined.remove(channel)

        CTL_RESET_STATE = 4028
        self.opus_encoder_private.opus_encoder_ctl(getattr(self.opus_encoder, '_state'), CTL_RESET_STATE)

        if self.v is not None:
            self.v.joined_updated()

    async def leave_voice(self, channel: discord.VoiceChannel) -> None:
        self.joined.remove(channel)

        futures = {voice_client.disconnect() for voice_client in typing.cast(typing.List[discord.VoiceClient], self.discord_client.voice_clients) if voice_client.channel == channel}
        if futures:
            await asyncio.wait(futures)

        if self.v is not None:
            self.v.joined_updated()

    def start_recording(self, hostapi: str, device: str) -> None:
        if self.input_stream is not None:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None

        hostapis = typing.cast(typing.Tuple[typing.Dict[str, typing.Any]], sounddevice.query_hostapis())
        devices = typing.cast(sounddevice.DeviceList, sounddevice.query_devices())

        device_id: int
        for idx, dev in enumerate(typing.cast(typing.Iterable[typing.Dict[str, typing.Any]], devices)):
            if dev['name'] == device and dev['max_input_channels'] > 0 and dev['hostapi'] < len(hostapis) and hostapis[typing.cast(int, dev['hostapi'])]['name'] == hostapi:
                device_id = idx
                break
        else:
            return

        self.input_stream = sounddevice.RawInputStream(samplerate=48000, blocksize=48000 * 20 // 1000, device=device_id, channels=2, dtype='float32', latency='low', callback=self._recording_callback, clip_off=True, dither_off=False, never_drop_input=False)
        try:
            self.input_stream.start()
        except Exception:
            self.input_stream.close()
            self.input_stream = None
            raise

    def set_bitrate(self, kbps: int) -> None:
        kbps = min(512, max(12, kbps))
        self.opus_encoder_private.opus_encoder_ctl(getattr(self.opus_encoder, '_state'), discord.opus.CTL_SET_BITRATE, kbps * 1000)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def _recording_callback(self, indata: typing.Any, frames: int, time: typing.Any, status: sounddevice.CallbackFlags) -> None:
        indata = bytes(indata)
        self.loop.call_soon_threadsafe(self._recording_callback_main_thread, indata)

    def _recording_callback_main_thread(self, indata: bytes) -> None:
        try:
            self.audio_queue.put_nowait(indata)
        except asyncio.queues.QueueFull:
            pass

    async def _encode_voice_loop(self) -> None:
        consecutive_silence = 0
        while self.running:
            buffer: typing.Optional[bytes] = await self.audio_queue.get()
            if buffer is None:
                return

            if self.muted:
                buffer = bytes(len(buffer))
                consecutive_silence += 1
            elif buffer.count(0) == len(buffer):
                consecutive_silence += 1
            else:
                consecutive_silence = 0

            speaking = discord.SpeakingState.voice if consecutive_silence <= 1 else discord.SpeakingState.none
            for voice_client in typing.cast(typing.List[discord.VoiceClient], self.discord_client.voice_clients):
                if voice_client.is_connected() and getattr(voice_client, '_dmb_speaking', discord.SpeakingState.none) != speaking:
                    asyncio.ensure_future(typing.cast(discord.gateway.DiscordVoiceWebSocket, voice_client.ws).speak(speaking))
                    setattr(voice_client, '_dmb_speaking', speaking)
            if consecutive_silence > 2:
                continue

            max_data_bytes = len(buffer)
            output = (ctypes.c_char * max_data_bytes)()
            output_len = self.opus_encoder_private.opus_encode_float(getattr(self.opus_encoder, '_state'), buffer, len(buffer) // 8, output, max_data_bytes)

            packet = bytes(output[:output_len])
            for voice_client in typing.cast(typing.List[discord.VoiceClient], self.discord_client.voice_clients):
                if voice_client.is_connected():
                    try:
                        voice_client.send_audio_packet(packet, encode=False)
                    except Exception as e:
                        print(e)

    async def run(self) -> None:
        self.login_status = 'Logging in…'
        if self.v is not None:
            self.v.login_status_updated()
        await self.discord_client.login(self.discord_bot_token, bot=True)

        self.login_status = 'Connecting to Discord server…'
        if self.v is not None:
            self.v.login_status_updated()
        asyncio.ensure_future(self.discord_client.connect())
        await self.discord_client.wait_until_ready()

        user: typing.Optional[discord.ClientUser] = typing.cast(typing.Any, self.discord_client.user)
        username = typing.cast(str, user.name) if user is not None else ''
        self.login_status = 'Logged in as: {}'.format(username)
        if self.v is not None:
            self.v.login_status_updated()

        self.guilds = await typing.cast(typing.Awaitable[typing.List[discord.Guild]], self.discord_client.fetch_guilds(limit=None).flatten())
        if self.v is not None:
            self.v.guilds_updated()

        await self._encode_voice_loop()

    async def stop(self) -> None:
        self.running = False
        await self.audio_queue.put(None)
        await self.discord_client.logout()