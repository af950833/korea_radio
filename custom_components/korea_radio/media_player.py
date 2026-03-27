import asyncio
import logging
import re
import socket
import time
import urllib.parse

import aiohttp
from aiohttp import web

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.const import CONF_NAME, STATE_IDLE, STATE_PLAYING, STATE_OFF
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, FIXED_URLS, STATIONS

_LOGGER = logging.getLogger(__name__)

# ----- Constants -----
DEFAULT_BITRATE = 128
RESUME_GRACE_SECONDS = 3
VOLUME_CACHE_DELAY = 0.8
FFMPEG_READ_SIZE = 8192


# ----- Helper Functions -----
def detect_host_ip(hass) -> str:
    """Detect the LAN IP address that cast devices can most likely reach."""
    candidates = []

    # Try internal_url
    try:
        internal_url = getattr(hass.config, "internal_url", None)
        if internal_url:
            parsed = urllib.parse.urlparse(internal_url)
            host = parsed.hostname
            if host and host not in ("localhost", "127.0.0.1"):
                candidates.append(host)
    except Exception as err:
        _LOGGER.debug("internal_url parse failed: %s", err)

    # Try api.base_url
    try:
        api = getattr(hass.config, "api", None)
        base_url = getattr(api, "base_url", None) if api else None
        if base_url:
            parsed = urllib.parse.urlparse(base_url)
            host = parsed.hostname
            if host and host not in ("localhost", "127.0.0.1"):
                candidates.append(host)
    except Exception as err:
        _LOGGER.debug("api.base_url parse failed: %s", err)

    # Socket-based detection
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
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
    """Fetch KBS radio stream URL."""
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
    """Fetch SBS radio stream URL."""
    sbs_ch = {
        "sbs_power": ("powerfm", "powerpc"),
        "sbs_love": ("lovefm", "lovepc"),
        "sbs_gorilla": ("sbsdmb", "sbsdmbpc"),
    }
    url = f"https://apis.sbs.co.kr/play-api/1.0/livestream/{sbs_ch[channel][1]}/{sbs_ch[channel][0]}?protocol=hls&ssl=Y"
    headers = {
        "Host": "apis.sbs.co.kr",
        "Connection": "keep-alive",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_16_0) AppleWebKit/537.36 (KHTML, like Gecko) GOREALRA/1.2.1 Chrome/85.0.4183.121 Electron/10.1.3 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://gorealraplayer.radio.sbs.co.kr",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://gorealraplayer.radio.sbs.co.kr/main.html?v=1.2.1",
        "Accept-Language": "ko",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return await resp.text()
    except Exception as err:
        _LOGGER.error("SBS URL error (%s): %s", channel, err)
    return None


async def async_get_mbc_url(channel: str, session: aiohttp.ClientSession) -> str | None:
    """Fetch MBC radio stream URL."""
    mbc_ch = {
        "mbc_fm4u": "mfm",
        "mbc_fm": "sfm",
        "mbc_allthatmusic": "chm",
    }
    url = f"https://sminiplay.imbc.com/aacplay.ashx?agent=webapp&channel={mbc_ch[channel]}&callback=jarvis.miniInfo.loadOnAirComplete"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://mini.imbc.com/",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()
            # Try AACLiveURL first
            match = re.search(r'"AACLiveURL":"([^"]+)"', text)
            if match:
                return match.group(1).replace("\\/", "/")
            # Fallback to m3u8
            match = re.search(r'https?://[^"]+\.m3u8[^"]*', text)
            if match:
                return match.group(0)
    except Exception as err:
        _LOGGER.error("MBC URL error (%s): %s", channel, err)
    return None


async def async_get_stream_url(key: str, session: aiohttp.ClientSession) -> str | None:
    """Get stream URL for a given station key."""
    if key in FIXED_URLS:
        return FIXED_URLS[key]
    if key.startswith("kbs_"):
        return await async_get_kbs_url(key, session)
    if key.startswith("sbs_"):
        return await async_get_sbs_url(key, session)
    if key.startswith("mbc_"):
        return await async_get_mbc_url(key, session)
    return None


# ----- FFmpeg Stream Server -----
class FFmpegStreamServer:
    """Manages ffmpeg process and serves the transcoded stream over HTTP."""

    __slots__ = (
        "hass", "original_url", "host_ip", "bitrate", "station_key", "process", "site", "port",
        "_app", "_runner", "_stop_called", "_on_stopped", "_stopped_notified",
        "_stderr_task", "_stream_task"
    )

    def __init__(self, hass, original_url, host_ip, bitrate, station_key=None, on_stopped=None):
        self.hass = hass
        self.original_url = original_url
        self.host_ip = host_ip
        self.bitrate = bitrate
        self.station_key = station_key
        self.process = None
        self.site = None
        self.port = None
        self._app = None
        self._runner = None
        self._stop_called = False
        self._on_stopped = on_stopped
        self._stopped_notified = False
        self._stderr_task = None
        self._stream_task = None

    async def _notify_stopped(self):
        """Notify the callback that the stream has stopped."""
        if self._on_stopped and not self._stopped_notified:
            self._stopped_notified = True
            try:
                result = self._on_stopped()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:
                _LOGGER.debug("FFmpeg stop callback failed: %s", err)

    async def start(self):
        """Start ffmpeg and HTTP server."""
        # Find free port
        with socket.socket() as sock:
            sock.bind(("", 0))
            self.port = sock.getsockname()[1]

        # Build ffmpeg command
        header_value = None

        if self.station_key == "obs":
            header_value = (
                "User-Agent: Mozilla/5.0\r\n"
                "Referer: https://www.obs.co.kr/\r\n"
                "Origin: https://www.obs.co.kr\r\n"
            )
        elif self.station_key and self.station_key.startswith("mbc_"):
            header_value = (
                "User-Agent: Mozilla/5.0\r\n"
                "Referer: http://mini.imbc.com/\r\n"
            )

        cmd = ["ffmpeg"]

        if header_value:
            cmd += ["-headers", header_value]

        cmd += [
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
        self._stderr_task = asyncio.create_task(self._log_stderr())

        # Setup HTTP server
        self._app = web.Application()
        self._app.router.add_get("/stream", self._handle_stream)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self.site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self.site.start()

        _LOGGER.info("FFmpeg 스트리밍 서버 시작: http://%s:%d/stream (%dkbps)", self.host_ip, self.port, self.bitrate)
        return True

    async def _log_stderr(self):
        """Log ffmpeg stderr output."""
        try:
            async for line in self.process.stderr:
                if line:
                    _LOGGER.debug("FFmpeg: %s", line.decode().strip())
        except Exception as err:
            _LOGGER.debug("FFmpeg stderr reader stopped: %s", err)

    async def _handle_stream(self, request):
        """Handle HTTP streaming request."""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)

        self._stream_task = asyncio.current_task()
        try:
            while True:
                data = await self.process.stdout.read(FFMPEG_READ_SIZE)
                if not data:
                    break
                await response.write(data)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, aiohttp.ClientConnectionResetError) as err:
            _LOGGER.debug("Stream client disconnected: %s", err)
            await self._notify_stopped()
            await self.stop()
        except Exception as err:
            _LOGGER.error("Stream error: %s", err)
            await self._notify_stopped()
            await self.stop()
        else:
            await self._notify_stopped()
            await self.stop()
        return response

    async def stop(self):
        """Stop ffmpeg and HTTP server."""
        if self._stop_called:
            return
        self._stop_called = True

        # Cancel background tasks
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()

        # Stop HTTP server
        if self.site:
            try:
                await self.site.stop()
            except RuntimeError:
                _LOGGER.debug("Site already stopped")
        if self._runner:
            await self._runner.cleanup()

        # Terminate ffmpeg
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
        await self._notify_stopped()
        _LOGGER.info("FFmpeg 스트리밍 서버 종료")

    @property
    def url(self):
        """Return the stream URL."""
        if self.port:
            return f"http://{self.host_ip}:{self.port}/stream"
        return None

    @property
    def is_running(self):
        """Check if the server is running."""
        return (
            self.process is not None
            and self.process.returncode is None
            and self.site is not None
            and self._runner is not None
            and not self._stop_called
        )


# ----- Media Player Entity -----
async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Korea Radio media player platform from a config entry."""
    config = {**entry.data, **entry.options}
    name = config.get(CONF_NAME, "Korea Radio")
    target_entity = config.get("target_media_player")
    bitrate = int(config.get("bitrate", DEFAULT_BITRATE))
    host_ip = detect_host_ip(hass)
    channels = config.get("channels", list(STATIONS.keys()))

    async_add_entities(
        [KoreaRadioMediaPlayer(target_entity, name, host_ip, bitrate, entry.entry_id, channels)]
    )


class KoreaRadioMediaPlayer(MediaPlayerEntity):
    """Media player entity for Korean radio streams."""

    __slots__ = (
        "_target_entity", "_attr_name", "_attr_icon", "_attr_unique_id", "_entry_id",
        "_host_ip", "_bitrate", "_state", "_current_station", "_media_title",
        "_ffmpeg_server", "_last_stream_url", "_volume_level_cache", "_volume_cache_task",
        "_manual_stop", "_resume_pending", "_resume_task", "_last_interrupt_ts",
        "_forced_off", "_enabled_stations"
    )

    def __init__(self, target_entity, name, host_ip, bitrate, entry_id, channels):
        self._target_entity = target_entity
        self._attr_name = name
        self._attr_icon = "mdi:radio"
        self._attr_unique_id = f"{DOMAIN}_{target_entity}_{entry_id}"
        self._entry_id = entry_id
        self._host_ip = host_ip
        self._bitrate = bitrate
        self._enabled_stations = channels or list(STATIONS.keys())
        self._state = STATE_IDLE
        self._current_station = None
        self._media_title = None
        self._ffmpeg_server = None
        self._last_stream_url = None
        self._volume_level_cache = None
        self._volume_cache_task = None

        self._manual_stop = False
        self._resume_pending = False
        self._resume_task = None
        self._last_interrupt_ts = 0.0
        self._forced_off = False

    # ---------- Properties ----------
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
        return [STATIONS[key] for key in self._enabled_stations if key in STATIONS]

    @property
    def source(self):
        return STATIONS.get(getattr(self, "_current_station", None))

    @property
    def media_title(self):
        return getattr(self, "_media_title", None)

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
            attrs["station_icon_url"] = f"/api/{DOMAIN}/icons/{self._current_station}.jpg"
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

    # ---------- Private Methods ----------
    def _ffmpeg_server_alive(self) -> bool:
        return self._ffmpeg_server is not None and self._ffmpeg_server.is_running

    async def _start_ffmpeg_server(self, station_key: str, stream_url: str) -> bool:
        """Start ffmpeg server for the given station and return the final URL."""
        self._ffmpeg_server = FFmpegStreamServer(
            self.hass,
            stream_url,
            self._host_ip,
            self._bitrate,
            station_key=station_key,
            on_stopped=self._handle_ffmpeg_stopped,
        )
        if await self._ffmpeg_server.start():
            final_url = self._ffmpeg_server.url
            _LOGGER.info("%s에 ffmpeg 변환 적용: %s (%dkbps)", station_key, final_url, self._bitrate)
            self._last_stream_url = final_url
            return True
        _LOGGER.error("%s ffmpeg 변환 실패, 원본 시도", station_key)
        self._ffmpeg_server = None
        self._last_stream_url = stream_url
        return False

    async def _stop_ffmpeg_server(self):
        """Stop and clean up ffmpeg server."""
        if self._ffmpeg_server:
            try:
                await self._ffmpeg_server.stop()
            except Exception as err:
                _LOGGER.error("Error stopping ffmpeg server: %s", err)
            self._ffmpeg_server = None
            self._last_stream_url = None

    async def _stop_target_media(self):
        """Stop media on the target player."""
        target_state = self.hass.states.get(self._target_entity)
        if target_state and target_state.state == "playing":
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_stop",
                    {"entity_id": self._target_entity},
                    blocking=False,
                )
                await asyncio.sleep(0.3)  # Short delay to allow stop to propagate
            except Exception as err:
                _LOGGER.debug("Error stopping media (ignored): %s", err)

    async def _clear_volume_cache_later(self, delay=VOLUME_CACHE_DELAY):
        """Clear volume cache after a delay."""
        try:
            await asyncio.sleep(delay)
            self._volume_level_cache = None
            self.async_write_ha_state()
        except asyncio.CancelledError:
            pass

    async def _handle_ffmpeg_stopped(self):
        """Handle ffmpeg server stopped unexpectedly."""
        _LOGGER.info(
            "FFmpeg stopped callback: manual_stop=%s current_station=%s resume_pending=%s",
            self._manual_stop,
            self._current_station,
            self._resume_pending,
        )
        self._ffmpeg_server = None
        self._last_stream_url = None
        self._state = STATE_IDLE
        self.async_write_ha_state()

        if self._manual_stop or not self._current_station:
            _LOGGER.info("Auto resume skipped: manual_stop=%s current_station=%s", self._manual_stop, self._current_station)
            return

        self._resume_pending = True
        self._last_interrupt_ts = time.monotonic()
        _LOGGER.info(
            "Auto resume scheduled: station=%s grace=%ss ts=%.3f",
            self._current_station,
            RESUME_GRACE_SECONDS,
            self._last_interrupt_ts,
        )

        if self._resume_task is None or self._resume_task.done():
            self._resume_task = self.hass.async_create_task(self._wait_and_resume_after_interrupts())

    async def _wait_and_resume_after_interrupts(self):
        """Wait for target player to be idle and resume playback."""
        _LOGGER.info("Auto resume loop started")
        try:
            while self._resume_pending:
                await asyncio.sleep(0.5)

                if self._manual_stop:
                    _LOGGER.info("Auto resume cancelled by manual stop")
                    self._resume_pending = False
                    return

                target_state = self.hass.states.get(self._target_entity)
                if not target_state:
                    _LOGGER.info("Auto resume waiting: target state unavailable")
                    continue

                quiet_for = time.monotonic() - self._last_interrupt_ts
                _LOGGER.info(
                    "Auto resume check: target_state=%s quiet_for=%.2f pending=%s ffmpeg_alive=%s",
                    target_state.state,
                    quiet_for,
                    self._resume_pending,
                    self._ffmpeg_server_alive(),
                )

                if target_state.state == STATE_PLAYING:
                    continue

                if quiet_for < RESUME_GRACE_SECONDS:
                    continue

                self._resume_pending = False
                _LOGGER.info("Auto resume firing for station: %s", self._current_station)
                await self.async_media_play()
                _LOGGER.info("Auto resume async_media_play() completed")
                return
        except asyncio.CancelledError:
            _LOGGER.info("Auto resume task cancelled")
            return

    # ---------- Volume Commands ----------
    async def async_set_volume_level(self, volume):
        self._volume_level_cache = volume
        self.async_write_ha_state()

        if self._volume_cache_task and not self._volume_cache_task.done():
            self._volume_cache_task.cancel()

        await self.hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": self._target_entity, "volume_level": volume},
            blocking=False,
        )

        self._volume_cache_task = self.hass.async_create_task(self._clear_volume_cache_later())

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

        self._volume_cache_task = self.hass.async_create_task(self._clear_volume_cache_later())

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

        self._volume_cache_task = self.hass.async_create_task(self._clear_volume_cache_later())

    async def async_mute_volume(self, mute):
        await self.hass.services.async_call(
            "media_player",
            "volume_mute",
            {"entity_id": self._target_entity, "is_volume_muted": mute},
            blocking=False,
        )
        self.async_write_ha_state()

    # ---------- Update ----------
    async def async_update(self):
        target_state = self.hass.states.get(self._target_entity)

        if self._forced_off:
            if target_state and target_state.state == STATE_PLAYING and self._ffmpeg_server_alive():
                self._forced_off = False
                self._state = STATE_PLAYING
            else:
                self._state = STATE_OFF
                return

        if not target_state:
            self._state = STATE_IDLE
            return

        if target_state.state == STATE_PLAYING and self._ffmpeg_server_alive():
            self._state = STATE_PLAYING
        elif self._state != STATE_IDLE and target_state.state not in (STATE_OFF, "idle", "paused", "standby"):
            self._state = target_state.state
        else:
            self._state = STATE_IDLE

    # ---------- Media Control ----------
    async def async_select_source(self, source):
        for key, name in STATIONS.items():
            if key not in self._enabled_stations:
                continue
            if name != source:
                continue

            # Stop current playback if any
            await self._stop_target_media()

            # Stop existing ffmpeg server
            await self._stop_ffmpeg_server()
            await asyncio.sleep(0.3)

            # Fetch stream URL
            session = async_get_clientsession(self.hass)
            stream_url = await async_get_stream_url(key, session)
            if not stream_url:
                _LOGGER.error("스트림 URL을 가져올 수 없음: %s", key)
                return

            # Start new ffmpeg server
            success = await self._start_ffmpeg_server(key, stream_url)
            final_url = self._last_stream_url

            # Reset flags
            self._manual_stop = False
            self._resume_pending = False
            self._forced_off = False
            self._current_station = key
            self._media_title = name
            self._state = STATE_PLAYING
            self.async_write_ha_state()

            # Play on target device
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
        if not self._current_station:
            _LOGGER.warning("Play requested but no station selected")
            return

        target_state = self.hass.states.get(self._target_entity)
        _LOGGER.info(
            "async_media_play called: station=%s target_state=%s ffmpeg_alive=%s last_stream=%s resume_pending=%s",
            self._current_station,
            target_state.state if target_state else None,
            self._ffmpeg_server_alive(),
            bool(self._last_stream_url),
            self._resume_pending,
        )

        if target_state and target_state.state == "playing" and self._ffmpeg_server_alive():
            _LOGGER.info("async_media_play ignored because target is already playing and ffmpeg is alive")
            return

        # Recreate ffmpeg server if needed
        if self._ffmpeg_server and not self._ffmpeg_server_alive():
            _LOGGER.info("Stopped ffmpeg server detected, recreating")
            self._ffmpeg_server = None
            self._last_stream_url = None

        if not self._ffmpeg_server:
            session = async_get_clientsession(self.hass)
            stream_url = await async_get_stream_url(self._current_station, session)
            if not stream_url:
                _LOGGER.error("스트림 URL을 가져올 수 없음: %s", self._current_station)
                return

            success = await self._start_ffmpeg_server(self._current_station, stream_url)
            if not success:
                _LOGGER.error("ffmpeg 변환 실패")
                return

        self._manual_stop = False
        self._resume_pending = False
        self._forced_off = False
        self._state = STATE_PLAYING
        self.async_write_ha_state()

        _LOGGER.info("Calling media_player.play_media target=%s url=%s", self._target_entity, self._last_stream_url)
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

    async def async_media_stop(self):
        self._manual_stop = True
        self._resume_pending = False

        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            self._resume_task = None

        await self._stop_target_media()
        await self._stop_ffmpeg_server()

        self._state = STATE_IDLE
        self.async_write_ha_state()

    async def async_turn_off(self):
        self._manual_stop = True
        self._resume_pending = False

        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            self._resume_task = None

        await self._stop_target_media()
        await self._stop_ffmpeg_server()

        self._forced_off = True
        self._state = STATE_OFF
        self.async_write_ha_state()
