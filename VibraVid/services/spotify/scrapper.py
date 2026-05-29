# 14.05.26

import logging

from VibraVid.services._base.object import Season, SeasonManager

from .client import JumoClient, resolve_format_id


logger = logging.getLogger(__name__)


class TrackInfo:
    def __init__(self, url: str, audio_format=None) -> None:
        self.url = str(url).strip()
        self.client = JumoClient()
        self._track_id: int | None = self._parse_id(self.url)
        self._format_id: int = resolve_format_id(audio_format)

        self.title: str = ""
        self.artist: str = ""
        self.album: str = ""
        self.year: str = ""
        self.genre: str = ""
        self.cover_url: str = ""
        self.stream_url: str = ""
        self.ext: str = "flac"
        self.track_num: int | None = None
        self.duration: int = 0

    @staticmethod
    def _parse_id(value: str) -> int | None:
        raw = value.strip()
        if raw.startswith("jumo:"):
            raw = raw.split(":", 1)[1]
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def fetch(self) -> None:
        """Fetch metadata and stream info from jumo-dl."""
        if self._track_id is None:
            raise ValueError(f"Cannot parse track id from url: {self.url!r}")
        
        logger.debug(f"Fetching track id={self._track_id} format_id={self._format_id}")
        data = self.client.fetch_stream(self._track_id, format_id=self._format_id)
        self._process_data(data)

    def _process_data(self, data: dict) -> None:
        meta = data.get("metadataTrack", {})
        album_meta = meta.get("album", {})

        self.title = meta.get("title", "Unknown Track")
        self.artist = (meta.get("performer", {}).get("name") or album_meta.get("artist", {}).get("name") or "")
        self.album = album_meta.get("title", "")

        release = (
            album_meta.get("release_date_original")
            or album_meta.get("release_date_stream")
            or album_meta.get("release_date_download")
            or ""
        )
        self.year = release[:4] if release else ""

        mime = data.get("mime_type", "")
        self.ext = "flac" if "flac" in mime else "mp3"
        self.stream_url = data.get("directUrl") or data.get("url") or ""

        cover = album_meta.get("image")
        self.cover_url = cover.get("large", "") if isinstance(cover, dict) else ""

        self.track_num = meta.get("track_number")
        self.duration = meta.get("duration", 0)

        genre = album_meta.get("genre")
        self.genre = genre.get("name", "") if isinstance(genre, dict) else ""

        logger.info(f"Track resolved: {self.artist} - {self.title}  ext={self.ext}  year={self.year}")

    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


class AlbumScraper:
    def __init__(self, album_id: str, audio_format=None) -> None:
        self.album_id = album_id
        self.client = JumoClient()
        self._format_id: int = resolve_format_id(audio_format)

        # Series-level metadata
        self.title: str = ""
        self.artist: str = ""
        self.year: str = ""
        self.genre: str = ""
        self.cover_url: str = ""

        # Compatibility aliases for shared series-oriented helpers.
        self.series_name: str = ""
        self.series_display_name: str = ""

        # seasons_manager is what process_season_selection reads
        self.seasons_manager: SeasonManager = SeasonManager()

        # Internal: disc_number -> [track_dict, ...]
        self._tracks_by_disc: dict[int, list[dict]] = {}

    def fetch(self) -> None:
        """Fetch album metadata and build seasons_manager."""
        logger.debug(f"AlbumScraper fetching album_id={self.album_id}")
        data = self.client.fetch_album(self.album_id)
        self._process_data(data)

    def _process_data(self, data: dict) -> None:
        self.title = data.get("title", "Unknown Album")
        self.artist = data.get("artist", {}).get("name", "")
        self.series_name = self.title
        self.series_display_name = self.title

        release = (
            data.get("release_date_original")
            or data.get("release_date_stream")
            or data.get("release_date_download")
            or ""
        )
        self.year = release[:4] if release else ""

        genre = data.get("genre")
        self.genre = genre.get("name", "") if isinstance(genre, dict) else ""

        cover = data.get("image")
        self.cover_url = cover.get("large", "") if isinstance(cover, dict) else ""

        # Group tracks by disc (media_number); default to disc 1
        self._tracks_by_disc.clear()
        for t in data.get("tracks", {}).get("items", []):
            disc = t.get("media_number") or 1
            self._tracks_by_disc.setdefault(disc, []).append(t)

        # Build SeasonManager — one Season per disc
        self.seasons_manager = SeasonManager()
        for disc_num in sorted(self._tracks_by_disc.keys()):
            season_name = (f"Disc {disc_num}" if len(self._tracks_by_disc) > 1 else self.title)
            season = Season(
                id=disc_num,
                number=disc_num,
                name=season_name,
            )
            self.seasons_manager.add(season)

        logger.info(f"AlbumScraper '{self.title}' by {self.artist}: {len(self.seasons_manager)} disc(s), {sum(len(v) for v in self._tracks_by_disc.values())} tracks")

    def getEpisodeSeasons(self, disc_number: int) -> list[dict]:
        """
        Return the track list for a given disc as episode dicts.

        Each dict has the keys that display_episodes_list and
        process_episode_download expect:
            id       - Jumo track integer id
            name     - track title
            number   - track number within the disc
            duration - seconds (int)
        """
        raw = self._tracks_by_disc.get(disc_number, [])
        episodes = []
        for t in raw:
            episodes.append({
                "id":       t.get("id"),
                "name":     t.get("title", "Unknown Track"),
                "number":   t.get("track_number"),
                "duration": t.get("duration", 0),
            })
        
        return episodes