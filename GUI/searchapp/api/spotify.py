# 17.04.26

import importlib
from typing import List, Optional

from .base import BaseStreamingAPI, Entries, Season

from VibraVid.services._base.site_loader import get_folder_name


class SpotifyAPI(BaseStreamingAPI):
	def __init__(self):
		super().__init__()
		self.site_name = "spotify"
		self._load_config()
		self._search_fn = None

	def _load_config(self):
		"""Load site configuration."""
		self.base_url = None

	def _get_search_fn(self):
		"""Lazy-load the service search function from the services package."""
		if self._search_fn is None:
			module = importlib.import_module(f"VibraVid.{get_folder_name()}.{self.site_name}")
			self._search_fn = getattr(module, "search")
		return self._search_fn

	def search(self, query: str) -> List[Entries]:
		"""Search for tracks and return a list of `Entries` for the GUI."""
		search_fn = self._get_search_fn()
		database = search_fn(query, get_onlyDatabase=True)

		results: List[Entries] = []
		if database and hasattr(database, "media_list"):
			items = list(database.media_list)
			for element in items:
				item_dict = element.__dict__.copy() if hasattr(element, "__dict__") else {}

				media_item = Entries(
					id=item_dict.get("id"),
					name=item_dict.get("name"),
					slug=item_dict.get("slug", ""),
					path_id=item_dict.get("path_id"),
					type=item_dict.get("type", "song"),
					url=item_dict.get("url"),
					poster=item_dict.get("image"),
					year=item_dict.get("year"),
					tmdb_id=item_dict.get("tmdb_id"),
					raw_data=item_dict,
				)
				results.append(media_item)

		return results

	def get_series_metadata(self, media_item: Entries) -> Optional[List[Season]]:
		"""Spotify is for single tracks — no series metadata."""
		return None

	def start_download(self, media_item: Entries, season: Optional[str] = None, episodes: Optional[str] = None) -> bool:
		"""Start a download for a selected track by delegating to the service.

		The services layer expects `direct_item` and optional `selections`.
		"""
		search_fn = self._get_search_fn()

		selections = None
		if season or episodes:
			selections = {"season": season, "episode": episodes}

		scrape_serie = self.get_cached_scraper(media_item)
		search_fn(direct_item=media_item.raw_data, selections=selections, scrape_serie=scrape_serie)
		return True