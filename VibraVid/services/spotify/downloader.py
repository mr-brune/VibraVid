# 14.05.26

import logging

from rich.console import Console

from VibraVid.utils import start_message
from VibraVid.services._base import site_constants, Entries
from VibraVid.services._base.tv_display_manager import map_song_path
from VibraVid.core.downloader.mp4 import MP4_Downloader
from VibraVid.core.ui.tracker import context_tracker
from VibraVid.core.muxing.helper.audio import process_song

from .scrapper import TrackInfo


console = Console()
logger = logging.getLogger(__name__)


def download_song(select_title: Entries) -> str | None:
    """
    Download a single song and run the full post-processing pipeline
    (tagging + optional FFmpeg conversion).

    Returns the final file path on success, None on failure.
    """
    start_message()
    console.print(f"\n[yellow]Download: [red]{site_constants.SITE_NAME} -> [cyan]{select_title.name}\n")

    # ── Resolve track metadata 
    track = TrackInfo(select_title.url)
    track.fetch()

    if not track.stream_url:
        console.print(f"[red]No stream URL available for: {select_title.name}")
        return None

    # ── Build destination path: Artist/Album/NN. Title.ext
    path_components, filename = map_song_path(
        artist=track.artist,
        album=track.album,
        title=track.title,
        year=track.year,
        track_number=track.track_num,
    )
    import os
    dest_path = os.path.join(
        site_constants.MUSIC_FOLDER,
        *path_components,
        f"{filename}.{track.ext}",
    )

    # ── Download
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
        logger.error(f"Spotify download error: {error}")
        return None

    return process_song(
        file_path=path,
        title=track.title,
        artist=track.artist,
        album=track.album,
        year=track.year,
        track_number=track.track_num,
        genre=track.genre,
        cover_url=track.cover_url
    )