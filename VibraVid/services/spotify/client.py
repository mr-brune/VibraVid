# 14.05.26

import logging
from typing import Any

from VibraVid.utils.os import internet_manager
from VibraVid.utils.http_client import create_client, get_userAgent


logger = logging.getLogger(__name__)
BASE_URL = "https://jumo-dl.pages.dev"
REGION = "US"

# Jumo audio format IDs. 27 = FLAC; 6 = MP3 320.
FORMAT_FLAC = 27
FORMAT_MP3  = 6
FORMAT_ID = FORMAT_FLAC

FORMAT_NAME_TO_ID = {
    "flac":    FORMAT_FLAC,
    "mp3":     FORMAT_MP3,
    "mp3_320": FORMAT_MP3,
}


def resolve_format_id(value) -> int:
    """Accept either an int (passthrough) or a name ('flac'/'mp3') and return the Jumo format_id."""
    if value is None or value == "":
        return FORMAT_ID
    if isinstance(value, int):
        return value
    return FORMAT_NAME_TO_ID.get(str(value).strip().lower(), FORMAT_ID)


def format_duration(seconds: int) -> str:
    """Convert seconds to M:SS string."""
    try:
        t = internet_manager.format_time(float(seconds))
    except Exception:
        m, s = divmod(int(seconds or 0), 60)
        return f"{m}:{s:02d}"

    if not t:
        return "0:00"
    parts = t.split(":", 1)
    try:
        minutes = str(int(parts[0]))
        return f"{minutes}:{parts[1]}"
    except Exception:
        return t


def _extract_year(album: dict) -> str:
    raw = (
        album.get("release_date_original")
        or album.get("release_date_stream")
        or album.get("release_date_download")
        or ""
    )
    return raw[:4] if raw else ""


def _extract_genre(album: dict) -> str:
    genre = album.get("genre")
    
    if isinstance(genre, dict):
        return genre.get("name", "")
    return ""


class JumoClient:
    def __init__(self) -> None:
        self.client = create_client(headers={
            "accept": "*/*",
            "accept-language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": f"{BASE_URL}/",
            "user-agent": get_userAgent()
        })

    def _get(self, endpoint: str, params: dict | None = None, timeout: int = 20) -> Any:
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        resp = self.client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def fetch_album(self, album_id: str) -> dict:
        """Fetch full album metadata including all track items."""
        return self._get("album", params={"album_id": album_id, "region": REGION})

    def search(self, query: str, limit: int = 20, search_type: str = "track") -> list[dict]:
        """
        Search for tracks and/or albums.

        Args:
            query: Search query string
            limit: Maximum number of results to return per type
            search_type: One of 'track', 'album', or 'both'
        """
        data = self._get("search", params={"query": query, "offset": 0, "limit": limit, "region": REGION})

        tracks: list[dict] = []
        raw_tracks: list[dict] = []
        raw_albums: list[dict] = []

        if search_type in ("track", "both"):
            if "tracks" in data and "items" in data["tracks"]:
                raw_tracks = data["tracks"]["items"]

        if search_type in ("album", "both"):
            if "albums" in data and "items" in data["albums"]:
                raw_albums = data["albums"]["items"]

        # Build album entries first so they appear grouped when search_type='both'
        for album in raw_albums:
            raw_tracks.append({"_album_mode": True, "album": album})

        for item in raw_tracks:
            if item.get("_album_mode"):
                album = item["album"]
                tracks.append({
                    "id": None,
                    "album_id": album.get("id"),
                    "title": album.get("title", "—"),
                    "artist": album.get("artist", {}).get("name", "—"),
                    "album": album.get("title", "—"),
                    "tracks_count": album.get("tracks_count", 0),
                    "duration": album.get("duration", 0),
                    "explicit": album.get("parental_warning", False),
                    "cover": album.get("image", {}).get("large", ""),
                    "track_num": None,
                    "qobuz_id": album.get("qobuz_id"),
                    "year": _extract_year(album),
                    "genre": _extract_genre(album),
                    "_raw": album,
                })
            
            else:
                album = item.get("album", {})
                tracks.append({
                    "id":        item.get("id"),
                    "title":     item.get("title", "—"),
                    "artist":    (
                        item.get("performer", {}).get("name")
                        or album.get("artist", {}).get("name", "—")
                    ),
                    "album":     album.get("title", "—"),
                    "duration":  item.get("duration", 0),
                    "explicit":  item.get("parental_warning", False),
                    "cover":     album.get("image", {}).get("large", ""),
                    "track_num": item.get("track_number"),
                    "qobuz_id":  album.get("qobuz_id"),
                    "year":      _extract_year(album),
                    "genre":     _extract_genre(album),
                    "_raw":      item,
                })

        return tracks

    def fetch_stream(self, track_id: int, format_id: int = FORMAT_ID) -> dict:
        return self._get("fetch", params={"track_id": track_id, "format_id": format_id, "region": REGION})