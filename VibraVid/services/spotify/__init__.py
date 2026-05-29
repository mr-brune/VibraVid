# 14.05.26

import logging

from rich.console import Console
from rich.prompt import Prompt

from VibraVid.utils import TVShowManager
from VibraVid.services._base import site_constants, EntriesManager, Entries
from VibraVid.services._base.site_search_manager import base_process_search_result, base_search
from VibraVid.services._base.tv_download_manager import process_season_selection, process_episode_download

from .client import JumoClient, format_duration
from .downloader import download_song, download_track_from_album
from .scrapper import AlbumScraper


indice = 17
_useFor = "song"
console = Console()
msg = Prompt()
logger = logging.getLogger(__name__)
entries_manager = EntriesManager()
table_show_manager = TVShowManager()


def title_search(query: str) -> int:
    """Search for both tracks and albums simultaneously."""
    entries_manager.clear()
    table_show_manager.clear()

    client = JumoClient()
    results = client.search(query, limit=25, search_type="both")

    for t in results:
        is_album = t.get("id") is None and t.get("album_id") is not None

        if is_album:
            name = (f"{t['artist']} - {t['title']}" if t.get("artist") not in ("—", "", None) else t["title"])
            entry = Entries(
                name=name,
                type="album",
                year=t.get("year", ""),
                image=t.get("cover", ""),
                url=f"jumo-album:{t['album_id']}",
            )
            entry.album = t.get("album", "")
            entry.duration = format_duration(t["duration"]) if t.get("duration") else "—"
            entry.explicit = "🅴" if t.get("explicit") else ""
            entry.genre = t.get("genre", "")
            tracks_count = t.get("tracks_count", 0)
            entry.tracks = str(tracks_count) if tracks_count else "—"

        else:
            if t["id"] is None:
                continue

            name = (f"{t['artist']} - {t['title']}" if t.get("artist") not in ("—", "", None) else t["title"])
            entry = Entries(
                name=name,
                type="song",
                year=t.get("year", ""),
                image=t.get("cover", ""),
                url=f"jumo:{t['id']}",
            )
            entry.album = t.get("album", "")
            entry.duration = format_duration(t["duration"]) if t.get("duration") else "—"
            entry.explicit = "🅴" if t.get("explicit") else ""
            entry.genre = t.get("genre", "")

        entries_manager.add(entry)

    return len(entries_manager)


def _build_album_scraper(select_title: Entries) -> AlbumScraper | None:
    """
    Instantiate and fetch an AlbumScraper from a selected album Entries item.
    Returns None if the album_id cannot be resolved.
    """
    raw_url = str(select_title.url).strip()
    album_id = raw_url.split(":", 1)[1] if ":" in raw_url else raw_url
    if not album_id:
        console.print(f"[red]Cannot resolve album id from url: {raw_url!r}")
        return None

    audio_format = getattr(select_title, "audio_format", None)
    scraper = AlbumScraper(album_id, audio_format=audio_format)
    scraper._audio_format_raw = audio_format

    try:
        scraper.fetch()
    except Exception as e:
        console.print(f"[red]Failed to fetch album '{select_title.name}': {e}")
        logger.error(f"AlbumScraper.fetch() error: {e}")
        return None

    return scraper


def download_series_album(select_title: Entries, season_selection=None, episode_selection=None, scrape_serie=None):
    """
    Builds (or reuses) an AlbumScraper and drives the standard
    """
    start_name = select_title.name if select_title else "Unknown"
    console.print(f"\n[yellow]Download album: [red]{site_constants.SITE_NAME} -> [cyan]{start_name}\n")

    # Build scraper if not already provided (e.g. from direct_item / GUI)
    if scrape_serie is None:
        scrape_serie = _build_album_scraper(select_title)
        if scrape_serie is None:
            return (None, False, "Could not load album metadata")

    seasons_count = len(scrape_serie.seasons_manager)

    def _download_episode_callback(season_number: int, download_all: bool, episode_selection=None):
        process_episode_download(
            index_season_selected=season_number,
            scrape_serie=scrape_serie,
            download_video_callback=lambda ep, sn, ei: download_track_from_album(ep, sn, ei, scrape_serie),
            download_all=download_all,
            episode_selection=episode_selection,
        )

    process_season_selection(
        scrape_serie=scrape_serie,
        seasons_count=seasons_count,
        season_selection=season_selection,
        episode_selection=episode_selection,
        download_episode_callback=_download_episode_callback,
    )

    entries_manager.clear()
    table_show_manager.clear()


def process_search_result(select_title, selections=None, scrape_serie=None):
    """Process search result — routes songs to download_song, albums to the series pipeline."""
    if select_title is None:
        console.print("[yellow]No title selected.")
        return False

    if selections and selections.get("audio_format"):
        select_title.audio_format = selections.get("audio_format")

    media_type = str(getattr(select_title, "type", "")).lower()

    if media_type == "album":
        if scrape_serie is None:
            scrape_serie = _build_album_scraper(select_title)
            if scrape_serie is None:
                return False

        return base_process_search_result(
            select_title=select_title,
            download_series_func=download_series_album,
            media_search_manager=entries_manager,
            table_show_manager=table_show_manager,
            selections=selections,
            scrape_serie=scrape_serie,
        )

    # Standard song
    return base_process_search_result(
        select_title=select_title,
        download_film_func=download_song,
        media_search_manager=entries_manager,
        table_show_manager=table_show_manager,
        selections=selections,
        scrape_serie=scrape_serie,
    )


def search(string_to_search: str = None, get_onlyDatabase: bool = False, direct_item: dict = None, selections: dict = None, scrape_serie=None):
    """Wrapper for the generalized search function."""
    return base_search(
        title_search_func=title_search,
        process_result_func=process_search_result,
        media_search_manager=entries_manager,
        table_show_manager=table_show_manager,
        site_name=site_constants.SITE_NAME,
        string_to_search=string_to_search,
        get_onlyDatabase=get_onlyDatabase,
        direct_item=direct_item,
        selections=selections,
        scrape_serie=scrape_serie,
    )