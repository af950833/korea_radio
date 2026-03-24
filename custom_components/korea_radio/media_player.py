import asyncio
import logging
import re
import socket
import urllib.parse

import aiohttp
from aiohttp import web

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.const import CONF_NAME, STATE_IDLE, STATE_PLAYING
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, FIXED_URLS, STATIONS

_LOGGER = logging.getLogger(__name__)


def detect_host_ip(hass) -> str:
    """Detect the LAN IP address that cast devices can most likely reach."""
    candidates = []

    try:
        internal_url = getattr(hass.config, "internal_url", None)
        if internal_url:
            parsed = urllib.parse.urlparse(internal_url)
            host = parsed.hostname
            if host and host not in ("localhost", "127.0.0.1"):
                candidates.append(host)
    except Exception as err:
        _LOGGER.debug("internal_url parse failed: %s", err)

    try:
        api = getattr(hass.config, "api", None)
        base_url = getattr(api, "base_url", None)
        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            host = parsed.hostname
            if host and host not in ("localhost", "127.0.0.1"):
                candidates.append(host)
    except Exception as err:
        _LOGGER.debug("api.base_url parse failed: %s", err)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and ip not in ("127.0.0.1", "0.0.0.0"):
            candidates.append(ip)
    except Exception as err:
        _LOGGER.debug("socket-based IP detection failed: %s", err)

    for candidate in candidates:
        if candidate:
            _LOGGER.info("자동 감지된 host_ip 사용: %s", candidate)
            return candidate

    _LOGGER.warning("host_ip 자동 감지 실패, localhost fallback 사용")
    return "127.0.0.1"


async def async_get_kbs_url(channel: str, session: aiohttp.ClientSession) -> str | None:
    kbs_ch = {
        "kbs_1radio": "21",
        "kbs_3radio": "23",
        "kbs_classic": "24",
        "kbs_cool": "25",
        "kbs_happy": "22",
    }
    url = f"https://cfpwwwapi.kbs.co.kr/api/v1/landing/live/channel_code/{kbs_ch[channel]}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://onair.kbs.co.kr/",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            for item in data.get("channel_item", []):
                if item.get("media_type") == "radio":
                    return item.get("service_url")
    except Exception as err:
        _LOGGER.error("KBS URL error (%s): %s", channel, err)
    return None


async def async_get_sbs_url(channel: str, session: aiohttp.ClientSession) -> str | None:
    sbs_ch = {
        "sbs_power": ("powerfm", "powerpc"),
        "sbs_love": ("lovefm", "lovepc"),
    }
    url = f"https://apis.sbs.co.kr/play-api/1.0/livestream/{sbs_ch[channel][1]}/{sbs_ch[channel][0]}?protocol=hls&ssl=Y"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_16_0) AppleWebKit/537.36",
        "Referer": "https://gorealraplayer.radio.sbs.co.kr/main.html",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return await resp.text()
    except Exception as err:
        _LOGGER.error("SBS URL error (%s): %s", channel, err)
    return None


async def async_get_mbc_url(channel: str, session: aiohttp.ClientSession) -> str | None:
    mbc_ch = {
        "mbc_fm4u": "mfm",
        "mbc_fm": "sfm",
    }
    url = f"https://sminiplay.imbc.com/aacplay.ashx?agent=webapp&channel={mbc_ch[channel]}&callback=jarvis.miniInfo.loadOnAirComplete"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://mini.imbc.com/",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()
            match = re.search(r'"AACLiveURL":"([^"]+)"', text)
            if match:
                raw_url = match.group(1).replace("\\/", "/")
                return raw_url
            match = re.search(r'https?://[^"]+\.m3u8[^"]*', text)
            if match:
                return match.group(0)
    except Exception as err:
        _LOGGER.error("MBC URL error (%s): %s", channel, err)
    return None


async def async_get_stream_url(key: str, session: aiohttp.ClientSession) -> str | None:
    if key in FIXED_URLS:
        return FIXED_URLS[key]
    if key.startswith("kbs_"):
        return await async_get_kbs_url(key, session)
    if key.startswith("sbs_"):
        return await async_get_sbs_url(key, session)
    if key.startswith("mbc_"):
        return await async_get_mbc_url(key, session)
    return None


class FFmpegStreamServer:
    def __init__(self, hass, original_url, host_ip, bitrate):
        self.hass = hass
        self.original_url = original_url
        self.host_ip = host_ip
        self.bitrate = bitrate
        self.process = None
        self.site = None
        self.port = None
        self._app = None
        self._runner = None
        self._stop_called = False

    async def start(self):
        sock = socket.socket()
        sock.bind(("", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        cmd = [
            "ffmpeg",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "-headers", "Referer: http://mini.imbc.com/",
            "-i", self.original_url,
            "-c:a", "mp3",
            "-b:a", f"{self.bitrate}k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "mp3",
            "pipe:1",
        ]
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_stderr())

        self._app = web.Application()
        self._app.router.add_get("/stream", self._handle_stream)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self.site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self.site.start()

        _LOGGER.info("FFmpeg 스트리밍 서버 시작: http://%s:%d/stream (%dkbps)", self.host_ip, self.port, self.bitrate)
        return True

    async def _log_stderr(self):
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                _LOGGER.debug("FFmpeg: %s", line.decode().strip())
        except Exception as err:
            _LOGGER.debug("FFmpeg stderr reader stopped: %s", err)

    async def _handle_stream(self, request):
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)
        try:
            while True:
                data = await self.process.stdout.read(8192)
                if not data:
                    break
                await response.write(data)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, aiohttp.ClientConnectionResetError) as err:
            _LOGGER.debug("Stream client disconnected: %s", err)
            await self.stop()
        except Exception as err:
            _LOGGER.error("Stream error: %s", err)
            await self.stop()
        return response

    async def stop(self):
        if self._stop_called:
            return
        self._stop_called = True

        if self.site:
            try:
                await self.site.stop()
            except RuntimeError:
                _LOGGER.debug("Site already stopped")
        if self._runner:
            await self._runner.cleanup()

        if self.process:
            try:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                _LOGGER.debug("Process already terminated")
            except Exception as err:
                _LOGGER.error("Error stopping ffmpeg: %s", err)

        self.site = None
        self._runner = None
        _LOGGER.info("FFmpeg 스트리밍 서버 종료")

    @property
    def url(self):
        if self.port:
            return f"http://{self.host_ip}:{self.port}/stream"
        return None


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Korea Radio media player platform from a config entry."""
    name = entry.data.get(CONF_NAME, "Korea Radio")
    target_entity = entry.data.get("target_media_player")
    bitrate = int(entry.data.get("bitrate", 128))
    host_ip = detect_host_ip(hass)

    async_add_entities([KoreaRadioMediaPlayer(target_entity, name, host_ip, bitrate, entry.entry_id)])


class KoreaRadioMediaPlayer(MediaPlayerEntity):
    def __init__(self, target_entity, name, host_ip, bitrate, entry_id):
        self._target_entity = target_entity
        self._attr_name = name
        self._attr_icon = "mdi:radio"
        self._attr_unique_id = f"{DOMAIN}_{target_entity}_{entry_id}"
        self._entry_id = entry_id
        self._host_ip = host_ip
        self._bitrate = bitrate
        self._state = STATE_IDLE
        self._current_station = None
        self._media_title = None
        self._ffmpeg_server = None
        self._last_stream_url = None
        self._volume_level_cache = None
        self._volume_cache_task = None

    @property
    def state(self):
        return self._state

    @property
    def supported_features(self):
        return (
            MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )

    @property
    def source_list(self):
        return list(STATIONS.values())

    @property
    def source(self):
        return STATIONS.get(self._current_station)

    @property
    def media_title(self):
        return self._media_title

    @property
    def media_image_url(self):
        if not self._current_station:
            return None
        return f"/api/{DOMAIN}/icons/{self._current_station}.jpg"

    @property
    def extra_state_attributes(self):
        attrs = {
            "detected_host_ip": self._host_ip,
            "target_media_player": self._target_entity,
            "bitrate": self._bitrate,
            "entry_id": self._entry_id,
        }
        if self._current_station:
            attrs["station_icon_url"] = f"/api/{DOMAIN}/icons/{self._current_station}.png"
        return attrs

    @property
    def volume_level(self):
        if self._volume_level_cache is not None:
            return self._volume_level_cache

        target_state = self.hass.states.get(self._target_entity)
        if not target_state:
            return None
        return target_state.attributes.get("volume_level")

    @property
    def is_volume_muted(self):
        target_state = self.hass.states.get(self._target_entity)
        if not target_state:
            return None
        return target_state.attributes.get("is_volume_muted")

    async def _clear_volume_cache_later(self, delay=0.8):
        try:
            await asyncio.sleep(delay)
            self._volume_level_cache = None
            self.async_write_ha_state()
        except asyncio.CancelledError:
            pass

    async def async_set_volume_level(self, volume):
        self._volume_level_cache = volume
        self.async_write_ha_state()

        if self._volume_cache_task and not self._volume_cache_task.done():
            self._volume_cache_task.cancel()

        await self.hass.services.async_call(
            "media_player",
            "volume_set",
            {
                "entity_id": self._target_entity,
                "volume_level": volume,
            },
            blocking=False,
        )

        self._volume_cache_task = self.hass.async_create_task(
            self._clear_volume_cache_later()
        )

    async def async_volume_up(self):
        target_state = self.hass.states.get(self._target_entity)
        current = target_state.attributes.get("volume_level", 0.0) if target_state else 0.0
        self._volume_level_cache = min(1.0, current + 0.05)
        self.async_write_ha_state()

        if self._volume_cache_task and not self._volume_cache_task.done():
            self._volume_cache_task.cancel()

        await self.hass.services.async_call(
            "media_player",
            "volume_up",
            {"entity_id": self._target_entity},
            blocking=False,
        )

        self._volume_cache_task = self.hass.async_create_task(
            self._clear_volume_cache_later()
        )

    async def async_volume_down(self):
        target_state = self.hass.states.get(self._target_entity)
        current = target_state.attributes.get("volume_level", 0.0) if target_state else 0.0
        self._volume_level_cache = max(0.0, current - 0.05)
        self.async_write_ha_state()

        if self._volume_cache_task and not self._volume_cache_task.done():
            self._volume_cache_task.cancel()

        await self.hass.services.async_call(
            "media_player",
            "volume_down",
            {"entity_id": self._target_entity},
            blocking=False,
        )

        self._volume_cache_task = self.hass.async_create_task(
            self._clear_volume_cache_later()
        )

    async def async_mute_volume(self, mute):
        await self.hass.services.async_call(
            "media_player",
            "volume_mute",
            {
                "entity_id": self._target_entity,
                "is_volume_muted": mute,
            },
            blocking=False,
        )
        self.async_write_ha_state()

    async def async_update(self):
        target_state = self.hass.states.get(self._target_entity)
        if not target_state:
            return

        if target_state.state == STATE_PLAYING:
            self._state = STATE_PLAYING
        elif self._state != STATE_IDLE and target_state.state not in ("off", "idle", "paused", "standby"):
            self._state = target_state.state
        else:
            self._state = STATE_IDLE

    async def async_select_source(self, source):
        for key, name in STATIONS.items():
            if name != source:
                continue

            target_state = self.hass.states.get(self._target_entity)
            if target_state and target_state.state == "playing":
                try:
                    await self.hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {"entity_id": self._target_entity},
                    )
                    await asyncio.sleep(0.5)
                except Exception as err:
                    _LOGGER.debug("Error stopping previous media (ignored): %s", err)

            if self._ffmpeg_server:
                try:
                    await self._ffmpeg_server.stop()
                except Exception as err:
                    _LOGGER.error("Error stopping previous ffmpeg server: %s", err)
                self._ffmpeg_server = None
                await asyncio.sleep(0.3)

            session = async_get_clientsession(self.hass)
            stream_url = await async_get_stream_url(key, session)
            if not stream_url:
                _LOGGER.error("스트림 URL을 가져올 수 없음: %s", key)
                return

            self._ffmpeg_server = FFmpegStreamServer(self.hass, stream_url, self._host_ip, self._bitrate)
            if await self._ffmpeg_server.start():
                final_url = self._ffmpeg_server.url
                _LOGGER.info("%s에 ffmpeg 변환 적용: %s (%dkbps)", key, final_url, self._bitrate)
            else:
                _LOGGER.error("%s ffmpeg 변환 실패, 원본 시도", key)
                final_url = stream_url

            self._current_station = key
            self._media_title = name
            self._last_stream_url = final_url
            self._state = STATE_PLAYING
            self.async_write_ha_state()

            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": self._target_entity,
                    "media_content_type": "audio/mpeg",
                    "media_content_id": final_url,
                },
                blocking=False,
            )
            break

    async def async_media_play(self):
        if self._current_station and self._last_stream_url:
            target_state = self.hass.states.get(self._target_entity)
            if target_state and target_state.state == "playing":
                return

            if not self._ffmpeg_server:
                session = async_get_clientsession(self.hass)
                stream_url = await async_get_stream_url(self._current_station, session)
                if not stream_url:
                    _LOGGER.error("스트림 URL을 가져올 수 없음: %s", self._current_station)
                    return

                self._ffmpeg_server = FFmpegStreamServer(self.hass, stream_url, self._host_ip, self._bitrate)
                if await self._ffmpeg_server.start():
                    final_url = self._ffmpeg_server.url
                    self._last_stream_url = final_url
                else:
                    _LOGGER.error("ffmpeg 변환 실패")
                    return

            self._state = STATE_PLAYING
            self.async_write_ha_state()

            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": self._target_entity,
                    "media_content_type": "audio/mpeg",
                    "media_content_id": self._last_stream_url,
                },
                blocking=False,
            )
        else:
            _LOGGER.warning("Play requested but no station selected")

    async def async_media_stop(self):
        target_state = self.hass.states.get(self._target_entity)
        if target_state and target_state.state == "playing":
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_stop",
                    {"entity_id": self._target_entity},
                )
                await asyncio.sleep(0.3)
            except Exception as err:
                _LOGGER.debug("Error stopping media (ignored): %s", err)

        if self._ffmpeg_server:
            try:
                await self._ffmpeg_server.stop()
            except Exception as err:
                _LOGGER.error("Error stopping ffmpeg server: %s", err)
            self._ffmpeg_server = None

        self._state = STATE_IDLE
        self.async_write_ha_state()

    async def async_turn_off(self):
        await self.async_media_stop()
