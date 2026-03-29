import asyncio
import json
import logging
import re
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html import unescape as html_unescape

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
SONG_UPDATE_INTERVAL = 10
PROGRAM_UPDATE_INTERVAL = 180

KBS_CHANNEL_CODES = {
    "kbs_1radio": "21",
    "kbs_3radio": "23",
    "kbs_classic": "24",
    "kbs_cool": "25",
    "kbs_happy": "22",
}


SBS_SIMPLE_CHANNELS = {
    "sbs_power": "powerfm",
    "sbs_love": "lovefm",
    "sbs_gorilla": "gorealram",
}

MBC_STREAM_CHANNELS = {
    "mbc_fm4u": "mfm",
    "mbc_fm": "sfm",
    "mbc_allthatmusic": "chm",
}

MBC_SCHEDULE_CHANNELS = {
    "mbc_fm": "STFM",
    "mbc_fm4u": "FM4U",
    "mbc_allthatmusic": "CHAM",
}

YTN_CHANNELS = {
    "ytn": {
        "schedule_url": "https://radio.ytn.co.kr/incfile/nowSchedule.xml",
        "method": "POST",
    },
    "ytn_radio": {
        "schedule_url": "https://radio.ytn.co.kr/incfile/nowSchedule.xml",
        "method": "POST",
    },
}


TBS_CHANNELS = {
    "tbsfm": "CH_A",
    "tbsefm": "CH_E",
}

TBN_CHANNELS = {
    "tbnfm": {
        "url": "https://www.tbn.or.kr/main.tbn?area_code=1",
    },
}

IFM_CHANNELS = {
    "ifm": {
        "url": "https://www.ifm.kr/onair/radio",
    },
}

OBS_CHANNELS = {
    "obs": {
        "url": "https://www.obs.co.kr/renewal/api/radio_schedule.php?type=desktop",
        "method": "POST",
    },
}


CBS_CHANNELS = {
    "cbs_fm": "fm",
    "cbs_music_fm": "musicFm",
    "cbs_joy4u": "joy4u",
}

EBS_CHANNELS = {
    "ebsfm": {
        "url": "https://ebr.ebs.co.kr/onair/scheduleNew.json?channelCodeString=RADIO&mode=newlist",
    },
}


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
    url = f"https://cfpwwwapi.kbs.co.kr/api/v1/landing/live/channel_code/{KBS_CHANNEL_CODES[channel]}"
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


async def async_get_kbs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch KBS current on-air program info."""
    channel_code = KBS_CHANNEL_CODES.get(channel)
    if not channel_code:
        return None

    url = (
        "https://static.api.kbs.co.kr/mediafactory/v1/schedule/onair_now"
        f"?local_station_code=00&channel_code={channel_code}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://onair.kbs.co.kr/",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json(content_type=None)

            if isinstance(data, list):
                data = data[0] if data else {}
            elif not isinstance(data, dict):
                return None

            schedules = data.get("schedules") or []
            if not schedules:
                return None

            current = schedules[0]
            return {
                "title": current.get("program_title") or current.get("programming_table_title"),
                "start": current.get("program_planned_start_time"),
                "end": current.get("program_planned_end_time"),
            }
    except Exception as err:
        _LOGGER.error("KBS now playing error (%s): %s", channel, err)

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


async def async_get_sbs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch SBS current on-air program and song info."""
    simple_channel = SBS_SIMPLE_CHANNELS.get(channel)
    if not simple_channel:
        return None

    url = f"https://gorealrainteraction.radio.sbs.co.kr/simple/{simple_channel}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.sbs.co.kr/",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json(content_type=None)
            payload = data.get("data", {}) if isinstance(data, dict) else {}
            onair = payload.get("onair", {}) if isinstance(payload, dict) else {}
            playlist = payload.get("playlist", {}) if isinstance(payload, dict) else {}

            if not onair:
                return None

            return {
                "title": onair.get("title"),
                "start": onair.get("start_time"),
                "end": onair.get("end_time"),
                "song": playlist.get("SONG_TITLE"),
                "artist": playlist.get("ARTIST_NAME") or playlist.get("DISPLAY_NAME"),
            }
    except Exception as err:
        _LOGGER.error("SBS now playing error (%s): %s", channel, err)

    return None


async def async_get_mbc_url(channel: str, session: aiohttp.ClientSession) -> str | None:
    """Fetch MBC radio stream URL."""
    url = f"https://sminiplay.imbc.com/aacplay.ashx?agent=webapp&channel={MBC_STREAM_CHANNELS[channel]}&callback=jarvis.miniInfo.loadOnAirComplete"
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



def _strip_jsonp_wrapper(text: str) -> str:
    """Strip a JSONP wrapper and return the inner JSON-like payload."""
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return text.strip()
    return text[start + 1:end].strip()


def _normalize_mbc_time(raw: str | None) -> str | None:
    """Normalize MBC time strings like 0000, 000000, 00000000 to HHMM."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    return None


def _mbc_time_in_range(now_hhmm: str, start_hhmm: str | None, end_hhmm: str | None) -> bool:
    """Return whether current HHMM falls in a start/end range, handling midnight crossover."""
    if not start_hhmm or not end_hhmm:
        return False

    if start_hhmm == end_hhmm:
        return True

    if start_hhmm <= end_hhmm:
        return start_hhmm <= now_hhmm < end_hhmm

    return now_hhmm >= start_hhmm or now_hhmm < end_hhmm


async def async_get_mbc_schedule_entries(
    channel: str,
    session: aiohttp.ClientSession,
) -> list[dict] | None:
    """Fetch and cache MBC schedule entries for the selected channel."""
    schedule_channel = MBC_SCHEDULE_CHANNELS.get(channel)
    if not schedule_channel:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://miniwebapp.imbc.com/index?channel={MBC_STREAM_CHANNELS.get(channel, 'sfm')}",
    }

    try:
        sched_url = "https://miniapi.imbc.com/Schedule/schedulelist?callback=__schedulelist"
        async with session.get(sched_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            sched_text = await resp.text()
        sched_payload = _strip_jsonp_wrapper(sched_text)
        schedule_data = json.loads(sched_payload)

        if isinstance(schedule_data, list):
            return [
                item for item in schedule_data
                if isinstance(item, dict) and item.get("Channel") == schedule_channel
            ]
    except Exception as err:
        _LOGGER.error("MBC schedule error (%s): %s", channel, err)

    return None


def _get_mbc_program_from_entries(entries: list[dict] | None) -> dict[str, str | None] | None:
    """Return current MBC program info from cached schedule entries."""
    if not entries:
        return None

    now_hhmm = time.strftime("%H%M")
    for item in entries:
        start = _normalize_mbc_time(item.get("StartTime"))
        end = _normalize_mbc_time(item.get("EndTime"))
        if _mbc_time_in_range(now_hhmm, start, end):
            return {
                "title": item.get("ProgramTitle"),
                "start": start,
                "end": end,
            }

    return None


async def async_get_mbc_song_info(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch MBC current song info."""
    schedule_channel = MBC_SCHEDULE_CHANNELS.get(channel)
    if not schedule_channel:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://miniwebapp.imbc.com/index?channel={MBC_STREAM_CHANNELS.get(channel, 'sfm')}",
    }

    song_title = None
    artist = None

    try:
        song_url = "https://miniapi.imbc.com/music/somitem?rtype=jsonp&callback=__somitem"
        async with session.get(song_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            song_text = await resp.text()
        song_payload = _strip_jsonp_wrapper(song_text)
        song_data = None
        try:
            song_data = json.loads(song_payload)
        except Exception:
            pass

        if isinstance(song_data, list):
            for item in song_data:
                if not isinstance(item, dict):
                    continue
                if item.get("Channel") != schedule_channel:
                    continue
                somitem = item.get("SomItem") or ""
                if somitem:
                    somitem = somitem.lstrip("♬").strip()

                if " - " in somitem:
                    song_title, artist = [part.strip() for part in somitem.split(" - ", 1)]
                elif somitem:
                    song_title = somitem.strip()
                break
    except Exception as err:
        _LOGGER.error("MBC song error (%s): %s", channel, err)

    if not any([song_title, artist]):
        return None

    return {
        "song": song_title,
        "artist": artist,
    }




async def async_get_ytn_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch YTN current on-air program info from nowSchedule.xml."""
    config = YTN_CHANNELS.get(channel)
    if not config:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://radio.ytn.co.kr/",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {}

    try:
        method = config.get("method", "GET").upper()
        timeout = aiohttp.ClientTimeout(total=5)
        if method == "POST":
            async with session.post(config["schedule_url"], headers=headers, data=data, timeout=timeout) as resp:
                text = await resp.text()
        else:
            async with session.get(config["schedule_url"], headers=headers, timeout=timeout) as resp:
                text = await resp.text()

        root = ET.fromstring(text)
        schedules = root.findall(".//schedule")
        if len(schedules) < 3:
            return None

        current = schedules[2]
        start = current.findtext("time")
        title = current.findtext("title")
        if title:
            title = title.replace("&amp;", "&").strip()

        end = None
        if len(schedules) >= 4:
            end = schedules[3].findtext("time")

        return {
            "title": title,
            "start": start.strip() if start else None,
            "end": end.strip() if end else None,
        }
    except Exception as err:
        _LOGGER.error("YTN now playing error (%s): %s", channel, err)

    return None


async def async_get_tbs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch TBS FM/eFM current on-air program info from live.do HTML."""
    channel_code = TBS_CHANNELS.get(channel)
    if not channel_code:
        return None

    url = f"http://tbs.seoul.kr/player/live.do?channelCode={channel_code}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://tbs.seoul.kr/fm/index.do",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()

        title_match = re.search(r'<span class="tit">\s*(.*?)\s*</span>', text, re.S)
        time_match = re.search(r'<span class="time">\s*(.*?)\s*</span>', text, re.S)

        title = html_unescape(title_match.group(1).strip()) if title_match else None
        time_text = html_unescape(time_match.group(1).strip()) if time_match else None

        start = None
        end = None
        if time_text and "~" in time_text:
            start, end = [part.strip() for part in time_text.split("~", 1)]

        if not title:
            return None

        return {
            "title": title,
            "start": start,
            "end": end,
        }
    except Exception as err:
        _LOGGER.error("TBS now playing error (%s): %s", channel, err)

    return None




async def async_get_tbn_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch TBN current on-air program info from main page HTML."""
    config = TBN_CHANNELS.get(channel)
    if not config:
        return None

    url = config["url"]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()

        matches = re.findall(
            r'<div\s+class="now-broad">.*?<dt>\s*(.*?)\s*</dt>.*?<dd>\s*(.*?)\s*</dd>',
            text,
            re.S,
        )
        if not matches:
            return None

        title, time_text = matches[0]
        title = html_unescape(title).strip()
        time_text = html_unescape(time_text).strip()

        start = None
        end = None
        if "~" in time_text:
            start, end = [part.strip() for part in time_text.split("~", 1)]

        if not title:
            return None

        return {
            "title": title,
            "start": start,
            "end": end,
        }
    except Exception as err:
        _LOGGER.error("TBN now playing error (%s): %s", channel, err)

    return None




async def async_get_ifm_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch IFM current on-air program info from onair page HTML."""
    config = IFM_CHANNELS.get(channel)
    if not config:
        return None

    url = config["url"]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()

        match = re.search(
            r'<div\s+style="position:\s*absolute;\s*color:\s*#fff;.*?text-align:\s*center;">\s*(.*?)\s*</div>',
            text,
            re.S,
        )
        if not match:
            return None

        title = html_unescape(match.group(1)).strip()
        if not title:
            return None

        return {
            "title": title,
            "start": None,
            "end": None,
        }
    except Exception as err:
        _LOGGER.error("IFM now playing error (%s): %s", channel, err)

    return None




async def async_get_obs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch OBS current on-air program info from radio_schedule JSON."""
    config = OBS_CHANNELS.get(channel)
    if not config:
        return None

    url = config["url"]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.obs.co.kr/radio/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        method = config.get("method", "GET").upper()
        timeout = aiohttp.ClientTimeout(total=5)
        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers("DEFAULT:@SECLEVEL=1")
        if method == "POST":
            async with session.post(url, headers=headers, data={}, timeout=timeout, ssl=ssl_context) as resp:
                data = await resp.json(content_type=None)
        else:
            async with session.get(url, headers=headers, timeout=timeout, ssl=ssl_context) as resp:
                data = await resp.json(content_type=None)

        if not isinstance(data, dict):
            return None

        title = data.get("name")
        start = data.get("stime")
        end = data.get("etime")
        if title:
            title = str(title).strip()
        if start:
            start = str(start).strip()
        if end:
            end = str(end).strip()

        if not title:
            return None

        return {
            "title": title,
            "start": start,
            "end": end,
        }
    except Exception as err:
        _LOGGER.error("OBS now playing error (%s): %s", channel, err)

    return None


def _get_cbs_schedule_type(channel: str | None) -> str | None:
    """Resolve CBS schedule type from station key."""
    if not channel:
        return None

    return CBS_CHANNELS.get(channel)


def _extract_cbs_entries(text: str) -> list[dict[str, str | bool | None]]:
    """Extract CBS schedule entries from HTML."""
    entries: list[dict[str, str | bool | None]] = []
    for match in re.finditer(r'<li\s+class="slide(?P<class_extra>[^"]*)">(?P<body>.*?)</li>', text, re.S):
        classes = match.group("class_extra") or ""
        body = match.group("body") or ""

        time_match = re.search(r'<div\s+class="time">\s*([^<]+?)\s*</div>', body, re.S)
        program_match = re.search(r'<div\s+class="program[^"]*">.*?<a[^>]*>\s*(.*?)\s*</a>', body, re.S)
        onair = 'btn-onair' in body or re.search(r'\bon\b', classes) is not None

        entry_time = html_unescape(time_match.group(1).strip()) if time_match else None
        title = html_unescape(re.sub(r'<[^>]+>', '', program_match.group(1)).strip()) if program_match else None
        if entry_time and title:
            entries.append({
                "time": entry_time,
                "title": title,
                "is_onair": onair,
            })

    return entries


async def async_get_cbs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch CBS current on-air program info from schedule HTML."""
    schedule_type = _get_cbs_schedule_type(channel)
    if not schedule_type:
        return None

    url = f"https://www.cbs.co.kr/schedule?type={schedule_type}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()

        entries = _extract_cbs_entries(text)
        if not entries:
            return None

        current_index = next((idx for idx, item in enumerate(entries) if item.get("is_onair")), None)
        if current_index is None:
            return None

        current = entries[current_index]
        next_entry = entries[current_index + 1] if current_index + 1 < len(entries) else None

        return {
            "title": current.get("title"),
            "start": current.get("time"),
            "end": next_entry.get("time") if next_entry else None,
        }
    except Exception as err:
        _LOGGER.error("CBS now playing error (%s): %s", channel, err)

    return None


async def async_get_ebs_nowplaying(
    channel: str,
    session: aiohttp.ClientSession,
) -> dict[str, str | None] | None:
    """Fetch EBS FM current on-air program info from JSON API."""
    config = EBS_CHANNELS.get(channel)
    if not config:
        return None

    url = config["url"]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://ebr.ebs.co.kr/radio/home",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json(content_type=None)

        if not isinstance(data, dict):
            return None

        now_program = data.get("nowProgram") or {}
        if not isinstance(now_program, dict):
            return None

        title = now_program.get("title")
        start = now_program.get("start")
        end = now_program.get("end")

        if title:
            title = str(title).strip()
        if start:
            start = str(start).strip()
        if end:
            end = str(end).strip()

        if not title:
            return None

        return {
            "title": title,
            "start": start,
            "end": end,
        }
    except Exception as err:
        _LOGGER.error("EBS now playing error (%s): %s", channel, err)

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
        "_host_ip", "_bitrate", "_state", "_current_station", "_media_title", "_media_artist",
        "_ffmpeg_server", "_last_stream_url", "_volume_level_cache", "_volume_cache_task",
        "_manual_stop", "_resume_pending", "_resume_task", "_last_interrupt_ts",
        "_forced_off", "_enabled_stations", "_now_playing_task", "_last_program_update_ts", "_mbc_cached_schedule_channel", "_mbc_schedule_entries",
        "_kbs_program_start", "_kbs_program_end",
        "_ytn_program_title", "_ytn_program_start", "_ytn_program_end",
        "_tbs_program_title", "_tbs_program_start", "_tbs_program_end",
        "_tbn_program_title", "_tbn_program_start", "_tbn_program_end",
        "_ifm_program_title", "_ifm_program_start", "_ifm_program_end",
        "_obs_program_title", "_obs_program_start", "_obs_program_end",
        "_cbs_program_title", "_cbs_program_start", "_cbs_program_end",
        "_ebs_program_title", "_ebs_program_start", "_ebs_program_end",
        "_sbs_program_title", "_sbs_program_start", "_sbs_program_end", "_sbs_song_title",
        "_sbs_artist", "_mbc_program_title", "_mbc_program_start", "_mbc_program_end",
        "_mbc_song_title", "_mbc_artist",
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
        self._media_artist = None
        self._ffmpeg_server = None
        self._last_stream_url = None
        self._volume_level_cache = None
        self._volume_cache_task = None
        self._now_playing_task = None
        self._last_program_update_ts = 0.0
        self._mbc_cached_schedule_channel = None
        self._mbc_schedule_entries = None
        self._kbs_program_start = None
        self._kbs_program_end = None
        self._ytn_program_title = None
        self._ytn_program_start = None
        self._ytn_program_end = None
        self._tbs_program_title = None
        self._tbs_program_start = None
        self._tbs_program_end = None
        self._tbn_program_title = None
        self._tbn_program_start = None
        self._tbn_program_end = None
        self._ifm_program_title = None
        self._ifm_program_start = None
        self._ifm_program_end = None
        self._obs_program_title = None
        self._obs_program_start = None
        self._obs_program_end = None
        self._cbs_program_title = None
        self._cbs_program_start = None
        self._cbs_program_end = None
        self._ebs_program_title = None
        self._ebs_program_start = None
        self._ebs_program_end = None
        self._sbs_program_title = None
        self._sbs_program_start = None
        self._sbs_program_end = None
        self._sbs_song_title = None
        self._sbs_artist = None
        self._mbc_program_title = None
        self._mbc_program_start = None
        self._mbc_program_end = None
        self._mbc_song_title = None
        self._mbc_artist = None

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
    def media_artist(self):
        return getattr(self, "_media_artist", None)

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
        if self._current_station and self._current_station.startswith("kbs_"):
            attrs["kbs_program_title"] = self._media_title
            attrs["kbs_program_start"] = self._kbs_program_start
            attrs["kbs_program_end"] = self._kbs_program_end
        if self._current_station in YTN_CHANNELS:
            attrs["ytn_program_title"] = self._ytn_program_title
            attrs["ytn_program_start"] = self._ytn_program_start
            attrs["ytn_program_end"] = self._ytn_program_end
        if self._current_station in TBS_CHANNELS:
            attrs["tbs_program_title"] = self._tbs_program_title
            attrs["tbs_program_start"] = self._tbs_program_start
            attrs["tbs_program_end"] = self._tbs_program_end
        if self._current_station in TBN_CHANNELS:
            attrs["tbn_program_title"] = self._tbn_program_title
            attrs["tbn_program_start"] = self._tbn_program_start
            attrs["tbn_program_end"] = self._tbn_program_end
        if self._current_station in IFM_CHANNELS:
            attrs["ifm_program_title"] = self._ifm_program_title
            attrs["ifm_program_start"] = self._ifm_program_start
            attrs["ifm_program_end"] = self._ifm_program_end
        if self._current_station in OBS_CHANNELS:
            attrs["obs_program_title"] = self._obs_program_title
            attrs["obs_program_start"] = self._obs_program_start
            attrs["obs_program_end"] = self._obs_program_end
        if _get_cbs_schedule_type(self._current_station):
            attrs["cbs_program_title"] = self._cbs_program_title
            attrs["cbs_program_start"] = self._cbs_program_start
            attrs["cbs_program_end"] = self._cbs_program_end
        if self._current_station in EBS_CHANNELS:
            attrs["ebs_program_title"] = self._ebs_program_title
            attrs["ebs_program_start"] = self._ebs_program_start
            attrs["ebs_program_end"] = self._ebs_program_end
        if self._current_station and self._current_station.startswith("sbs_"):
            attrs["sbs_program_title"] = self._sbs_program_title
            attrs["sbs_program_start"] = self._sbs_program_start
            attrs["sbs_program_end"] = self._sbs_program_end
            attrs["sbs_song_title"] = self._sbs_song_title
            attrs["sbs_artist"] = self._sbs_artist
        if self._current_station and self._current_station.startswith("mbc_"):
            attrs["mbc_program_title"] = self._mbc_program_title
            attrs["mbc_program_start"] = self._mbc_program_start
            attrs["mbc_program_end"] = self._mbc_program_end
            attrs["mbc_song_title"] = self._mbc_song_title
            attrs["mbc_artist"] = self._mbc_artist
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

    def _set_default_media_title(self):
        """Set default media metadata based on current station."""
        self._media_title = STATIONS.get(self._current_station)
        self._media_artist = None
        if not (self._current_station and self._current_station.startswith("kbs_")):
            self._kbs_program_start = None
            self._kbs_program_end = None
        if not (self._current_station in YTN_CHANNELS):
            self._ytn_program_title = None
            self._ytn_program_start = None
            self._ytn_program_end = None
        if not (self._current_station in TBS_CHANNELS):
            self._tbs_program_title = None
            self._tbs_program_start = None
            self._tbs_program_end = None
        if not (self._current_station in TBN_CHANNELS):
            self._tbn_program_title = None
            self._tbn_program_start = None
            self._tbn_program_end = None
        if not (self._current_station in IFM_CHANNELS):
            self._ifm_program_title = None
            self._ifm_program_start = None
            self._ifm_program_end = None
        if not (self._current_station in OBS_CHANNELS):
            self._obs_program_title = None
            self._obs_program_start = None
            self._obs_program_end = None
        if not _get_cbs_schedule_type(self._current_station):
            self._cbs_program_title = None
            self._cbs_program_start = None
            self._cbs_program_end = None
        if not (self._current_station in EBS_CHANNELS):
            self._ebs_program_title = None
            self._ebs_program_start = None
            self._ebs_program_end = None
        if not (self._current_station and self._current_station.startswith("sbs_")):
            self._sbs_program_title = None
            self._sbs_program_start = None
            self._sbs_program_end = None
            self._sbs_song_title = None
            self._sbs_artist = None
        if not (self._current_station and self._current_station.startswith("mbc_")):
            self._mbc_program_title = None
            self._mbc_program_start = None
            self._mbc_program_end = None
            self._mbc_song_title = None
            self._mbc_artist = None
            self._mbc_schedule_entries = None
            self._mbc_cached_schedule_channel = None

    async def _update_kbs_now_playing(self, force: bool = False):
        """Fetch and apply KBS on-air program info every 3 minutes."""
        if not (self._current_station and self._current_station.startswith("kbs_")):
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_kbs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._media_title = info["title"]
            self._media_artist = None
            self._kbs_program_start = info.get("start")
            self._kbs_program_end = info.get("end")
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_ytn_now_playing(self, force: bool = False):
        """Fetch and apply YTN on-air program info."""
        if self._current_station not in YTN_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_ytn_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._ytn_program_title = info.get("title")
            self._ytn_program_start = info.get("start")
            self._ytn_program_end = info.get("end")
            self._media_title = self._ytn_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_tbs_now_playing(self, force: bool = False):
        """Fetch and apply TBS FM/eFM on-air program info."""
        if self._current_station not in TBS_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_tbs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._tbs_program_title = info.get("title")
            self._tbs_program_start = info.get("start")
            self._tbs_program_end = info.get("end")
            self._media_title = self._tbs_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_tbn_now_playing(self, force: bool = False):
        """Fetch and apply TBN on-air program info."""
        if self._current_station not in TBN_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_tbn_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._tbn_program_title = info.get("title")
            self._tbn_program_start = info.get("start")
            self._tbn_program_end = info.get("end")
            self._media_title = self._tbn_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_ifm_now_playing(self, force: bool = False):
        """Fetch and apply IFM on-air program info."""
        if self._current_station not in IFM_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_ifm_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._ifm_program_title = info.get("title")
            self._ifm_program_start = info.get("start")
            self._ifm_program_end = info.get("end")
            self._media_title = self._ifm_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_obs_now_playing(self, force: bool = False):
        """Fetch and apply OBS on-air program info."""
        if self._current_station not in OBS_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_obs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._obs_program_title = info.get("title")
            self._obs_program_start = info.get("start")
            self._obs_program_end = info.get("end")
            self._media_title = self._obs_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_cbs_now_playing(self, force: bool = False):
        """Fetch and apply CBS on-air program info."""
        if not _get_cbs_schedule_type(self._current_station):
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_cbs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._cbs_program_title = info.get("title")
            self._cbs_program_start = info.get("start")
            self._cbs_program_end = info.get("end")
            self._media_title = self._cbs_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_ebs_now_playing(self, force: bool = False):
        """Fetch and apply EBS on-air program info."""
        if self._current_station not in EBS_CHANNELS:
            return

        now_ts = time.monotonic()
        if not force and (now_ts - self._last_program_update_ts) < PROGRAM_UPDATE_INTERVAL:
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_ebs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._ebs_program_title = info.get("title")
            self._ebs_program_start = info.get("start")
            self._ebs_program_end = info.get("end")
            self._media_title = self._ebs_program_title
            self._media_artist = None
            self._last_program_update_ts = now_ts
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _update_sbs_now_playing(self, force: bool = False):
        """Fetch and apply SBS on-air program and song info."""
        if not (self._current_station and self._current_station.startswith("sbs_")):
            return

        session = async_get_clientsession(self.hass)
        info = await async_get_sbs_nowplaying(self._current_station, session)
        if info and info.get("title"):
            self._sbs_program_title = info["title"]
            self._sbs_program_start = info.get("start")
            self._sbs_program_end = info.get("end")
            self._sbs_song_title = info.get("song")
            self._sbs_artist = info.get("artist")

            if self._sbs_song_title and self._sbs_artist:
                song_line = f"{self._sbs_artist} - {self._sbs_song_title}"
                self._media_title = f"{self._sbs_program_title} | {song_line}"
                self._media_artist = song_line
            elif self._sbs_song_title:
                self._media_title = f"{self._sbs_program_title} | {self._sbs_song_title}"
                self._media_artist = self._sbs_song_title
            else:
                self._media_title = self._sbs_program_title
                self._media_artist = None
        else:
            self._set_default_media_title()

        self.async_write_ha_state()

    async def _refresh_mbc_program_from_cache(self):
        """Refresh MBC program info from cached schedule entries."""
        info = _get_mbc_program_from_entries(self._mbc_schedule_entries)
        if info:
            self._mbc_program_title = info.get("title")
            self._mbc_program_start = info.get("start")
            self._mbc_program_end = info.get("end")
        else:
            self._mbc_program_title = STATIONS.get(self._current_station)
            self._mbc_program_start = None
            self._mbc_program_end = None

    async def _load_mbc_schedule_cache(self, force: bool = False):
        """Load MBC schedule once when the channel starts and cache it."""
        if not (self._current_station and self._current_station.startswith("mbc_")):
            return

        if (
            not force
            and self._mbc_cached_schedule_channel == self._current_station
            and self._mbc_schedule_entries is not None
        ):
            return

        session = async_get_clientsession(self.hass)
        entries = await async_get_mbc_schedule_entries(self._current_station, session)
        self._mbc_cached_schedule_channel = self._current_station
        self._mbc_schedule_entries = entries or []
        await self._refresh_mbc_program_from_cache()

    async def _update_mbc_now_playing(self, force: bool = False):
        """Fetch and apply MBC song info while using cached schedule info."""
        if not (self._current_station and self._current_station.startswith("mbc_")):
            return

        await self._load_mbc_schedule_cache(force=force)
        await self._refresh_mbc_program_from_cache()

        session = async_get_clientsession(self.hass)
        song_info = await async_get_mbc_song_info(self._current_station, session)
        if song_info:
            self._mbc_song_title = song_info.get("song")
            self._mbc_artist = song_info.get("artist")
        else:
            self._mbc_song_title = None
            self._mbc_artist = None

        title = self._mbc_program_title or STATIONS.get(self._current_station)
        if self._mbc_song_title and self._mbc_artist:
            song_line = f"{self._mbc_artist} - {self._mbc_song_title}"
            self._media_title = f"{title} | {song_line}"
            self._media_artist = song_line
        elif self._mbc_song_title:
            self._media_title = f"{title} | {self._mbc_song_title}"
            self._media_artist = self._mbc_song_title
        else:
            self._media_title = title
            self._media_artist = None

        self.async_write_ha_state()

    async def _now_playing_loop(self):
        """Periodically refresh supported on-air info while playing."""
        try:
            while True:
                if not self._current_station:
                    return

                if self._state == STATE_OFF:
                    return

                if self._current_station.startswith("kbs_"):
                    await self._update_kbs_now_playing(force=False)
                elif self._current_station in YTN_CHANNELS:
                    await self._update_ytn_now_playing(force=False)
                elif self._current_station in TBS_CHANNELS:
                    await self._update_tbs_now_playing(force=False)
                elif self._current_station in TBN_CHANNELS:
                    await self._update_tbn_now_playing(force=False)
                elif self._current_station in IFM_CHANNELS:
                    await self._update_ifm_now_playing(force=False)
                elif self._current_station in OBS_CHANNELS:
                    await self._update_obs_now_playing(force=False)
                elif _get_cbs_schedule_type(self._current_station):
                    await self._update_cbs_now_playing(force=False)
                elif self._current_station in EBS_CHANNELS:
                    await self._update_ebs_now_playing(force=False)
                elif self._current_station.startswith("sbs_"):
                    await self._update_sbs_now_playing(force=False)
                elif self._current_station.startswith("mbc_"):
                    await self._update_mbc_now_playing(force=False)
                else:
                    return

                await asyncio.sleep(SONG_UPDATE_INTERVAL)
        except asyncio.CancelledError:
            return

    async def _start_now_playing_updates(self):
        """Start periodic now-playing updates for supported stations."""
        await self._stop_now_playing_updates()

        if not self._current_station:
            self._set_default_media_title()
            self.async_write_ha_state()
            return

        if self._current_station.startswith("kbs_"):
            await self._update_kbs_now_playing(force=True)
        elif self._current_station in YTN_CHANNELS:
            await self._update_ytn_now_playing(force=True)
        elif self._current_station in TBS_CHANNELS:
            await self._update_tbs_now_playing(force=True)
        elif self._current_station in TBN_CHANNELS:
            await self._update_tbn_now_playing(force=True)
        elif self._current_station in IFM_CHANNELS:
            await self._update_ifm_now_playing(force=True)
        elif self._current_station in OBS_CHANNELS:
            await self._update_obs_now_playing(force=True)
        elif _get_cbs_schedule_type(self._current_station):
            await self._update_cbs_now_playing(force=True)
        elif self._current_station in EBS_CHANNELS:
            await self._update_ebs_now_playing(force=True)
        elif self._current_station.startswith("sbs_"):
            await self._update_sbs_now_playing(force=True)
        elif self._current_station.startswith("mbc_"):
            await self._update_mbc_now_playing(force=True)
        else:
            self._set_default_media_title()
            self.async_write_ha_state()
            return

        self._now_playing_task = self.hass.async_create_task(self._now_playing_loop())

    async def _stop_now_playing_updates(self):
        """Stop periodic KBS now-playing updates."""
        if self._now_playing_task and not self._now_playing_task.done():
            self._now_playing_task.cancel()
            try:
                await self._now_playing_task
            except asyncio.CancelledError:
                pass
        self._now_playing_task = None

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

            await self._stop_now_playing_updates()

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
            self._set_default_media_title()
            self._state = STATE_PLAYING
            self.async_write_ha_state()

            await self._start_now_playing_updates()

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

        await self._start_now_playing_updates()

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

        await self._stop_now_playing_updates()
        await self._stop_target_media()
        await self._stop_ffmpeg_server()

        self._set_default_media_title()
        self._state = STATE_IDLE
        self.async_write_ha_state()

    async def async_turn_off(self):
        self._manual_stop = True
        self._resume_pending = False

        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
            self._resume_task = None

        await self._stop_now_playing_updates()
        await self._stop_target_media()
        await self._stop_ffmpeg_server()

        self._set_default_media_title()
        self._forced_off = True
        self._state = STATE_OFF
        self.async_write_ha_state()
