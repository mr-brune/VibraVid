# 06.06.25

from django.apps import AppConfig


class SearchappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "searchapp"

    def ready(self) -> None:
        try:
            from .watchlist_auto import start_watchlist_auto_loop

            start_watchlist_auto_loop()
        except Exception as exc:
            print(f"[WatchlistAuto] Failed to start: {exc}")

        try:
            from .arr_auto import start_arr_auto_loop

            start_arr_auto_loop()
        except Exception as exc:
            print(f"[ARR] Failed to start auto loop: {exc}")