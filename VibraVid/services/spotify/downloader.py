# 14.05.26

import os
import logging

from rich.console import Console

from VibraVid.utils import start_message
from VibraVid.services._base import site_constants, Entries
from VibraVid.services._base.tv_display_manager import map_song_path
from VibraVid.core.downloader.mp4 import MP4_Downloader
from VibraVid.core.ui.tracker import context_tracker
from VibraVid.core.muxing.helper.audio import process_song
from .scrapper import TrackInfo, AlbumScraper


console = Console()
logger = logging.getLogger(__name__)

def download_song(select_title: Entries) -> str | None:
    """
    Download a single song and run the full post-processing pipeline.
    Returns the final file path on success, None on failure.
    """
    start_message()
    console.print(f"\n[yellow]Download: [red]{site_constants.SITE_NAME} -> [cyan]{select_title.name}\n")

    requested_format = getattr(select_title, "audio_format", None)
    track = TrackInfo(select_title.url, audio_format=requested_format)
    track.fetch()

    if not track.stream_url:
        console.print(f"[red]No stream URL available for: {select_title.name}")
        return None

    path_components, filename = map_song_path(
        artist=track.artist,
        album=track.album,
        title=track.title,
        year=track.year,
        track_number=track.track_num,
    )
    dest_path = os.path.join(
        site_constants.MUSIC_FOLDER,
        *path_components,
        f"{filename}.{track.ext}",
    )

    path, stopped, error = MP4_Downloader(
        url=track.stream_url,
        path=dest_path,
        download_id=context_tracker.download_id,
        site_name=site_constants.SITE_NAME,
        label="Audio",
    )
    if not path or stopped:
        return None
    if error:
        logger.error(f"Download error: {error}")
        return None

    return process_song(
        file_path=path,
        title=track.title,
        artist=track.artist,
        album=track.album,
        year=track.year,
        track_number=track.track_num,
        genre=track.genre,
        cover_url=track.cover_url,
    )


def download_track_from_album(episode_dict: dict, disc_number: int, episode_index: int, scrape_serie: AlbumScraper) -> tuple:
    """
    Download a single track that belongs to an album.
    """
    track_id = episode_dict.get("id") if isinstance(episode_dict, dict) else getattr(episode_dict, "id", None)
    track_name = episode_dict.get("name", "Unknown Track") if isinstance(episode_dict, dict) else getattr(episode_dict, "name", "Unknown Track")

    if track_id is None:
        msg = f"No track id for: {track_name}"
        logger.error(msg)
        return (None, False, msg)

    audio_format = getattr(scrape_serie, "_audio_format_raw", None)
    track = TrackInfo(f"jumo:{track_id}", audio_format=audio_format)

    try:
        track.fetch()
    except Exception as e:
        msg = f"Failed to fetch stream for '{track_name}': {e}"
        logger.error(msg)
        return (None, False, msg)

    if not track.stream_url:
        msg = f"No stream URL for: {track_name}"
        logger.error(msg)
        return (None, False, msg)

    path_components, filename = map_song_path(
        artist=track.artist or scrape_serie.artist,
        album=track.album or scrape_serie.title,
        title=track.title,
        year=track.year or scrape_serie.year,
        track_number=track.track_num,
    )

    dest_path = os.path.join(site_constants.MUSIC_FOLDER, *path_components, f"{filename}.{track.ext}")
    path, stopped, error = MP4_Downloader(
        url=track.stream_url,
        path=dest_path,
        download_id=context_tracker.download_id,
        site_name=site_constants.SITE_NAME,
        label=f"Track {track.track_num or episode_index}",
    )

    if stopped:
        return (None, True, None)
    
    if not path or error:
        return (None, False, error or f"Download failed for '{track.title}'")

    result_path = process_song(
        file_path=path,
        title=track.title,
        artist=track.artist,
        album=track.album,
        year=track.year,
        track_number=track.track_num,
        genre=track.genre,
        cover_url=track.cover_url,
    )

    return (result_path, False, None)