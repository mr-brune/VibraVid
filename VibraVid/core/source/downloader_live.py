# 09.04.26

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from VibraVid.core.muxing.helper.video import binary_merge_segments
from VibraVid.core.ui.bar_manager import console
from VibraVid.core.source.download_utils import (format_size as _fmt_size, format_speed as _fmt_speed, fmt_dur as _fmt_dur)
from VibraVid.core.manifest.mpd import DashParser
from VibraVid.core.decryptor import Decryptor
from VibraVid.utils.http_client import create_client
from VibraVid.utils import config_manager

from ._hls_utils import hls_base_url, parse_hls_live_playlist
from ._stream_helpers import detect_seg_ext
from ..decryptor._segment_crypto import decrypt_aes128


logger = logging.getLogger("manual")
REQUEST_TIMEOUT: int = config_manager.config.get_int("REQUESTS", "timeout")
MAX_EMPTY_POLLS: int = config_manager.config.get_int("DOWNLOAD", "live_max_empty_polls")


def _sleep_interruptible(seconds: float, stop_check, poll: float = 0.25) -> None:
    """Sleep *seconds* but wake early when stop_check() returns True."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_check():
            break
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def _emit_live_progress(bar_manager, task_key: str, seg_done: int, total_bytes: int, speed_bps: float, elapsed_dur: float = 0.0) -> None:
    duration_display = f"{_fmt_dur(elapsed_dur)}/~" if elapsed_dur > 0 else ""
    bar_manager.handle_progress_line({
        "task_key": task_key,
        "pct":      min(98, seg_done),
        "segments": f"{seg_done}/~",
        "size":     f"{_fmt_size(total_bytes)}/~",
        "speed":    _fmt_speed(speed_bps),
        "duration": duration_display,
    })


def _emit_merge_progress(bar_manager, task_key: str, seg_done: int, merge_size: int) -> None:
    bar_manager.handle_progress_line({
        "task_key": task_key,
        "pct":      100,
        "segments": f"{seg_done}/{seg_done}",
        "size":     f"{_fmt_size(merge_size)}/{_fmt_size(merge_size)}",
        "speed":    "Merge",
    })


class LiveDownloadMixin:
    def _live_download_batch(self, dl_batch: List[Dict], stream_dir: Path, headers: Dict, stream, progress_cb=None) -> List[Path]:
        self._run_dl(
            dl_batch,
            stream_dir,
            headers,
            progress_cb=progress_cb,
            stream=stream,
            event_cb=None,
        )

        # Reconstruct the expected path for each entry in dl_batch.
        ordered: List[Path] = []
        for seg in dl_batch:
            seg_ext = detect_seg_ext(seg.get("url", ""), default="mp4")
            if seg_ext == "m4s":
                seg_ext = "mp4"
            ordered.append(stream_dir / f"seg_{seg['number']:05d}.{seg_ext}")

        return ordered

    def _download_hls_live_stream(
        self,
        stream,
        bar_manager,
        live_decryption: bool = False,
        *,
        first_content: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        """
        Download a live HLS stream by polling the variant playlist and downloading each new batch of segments as they appear.
        """
        playlist_url: str  = stream.playlist_url
        all_headers:  Dict = self._build_headers()
        task_key:     str  = self._stream_task_key(stream)
        stream_dir:   Path = self._make_stream_dir(stream, "hls")

        key_cache:     Dict[str, bytes] = {}
        all_paths:     List[Path]       = []   # ordered: [init, media_1, media_2, …]
        seen_seg_keys: set              = set()

        # 0 is reserved for the init segment; media segments start at 1.
        seg_index: int = 1
        seg_done: int = 0
        total_bytes: int = 0
        elapsed_dur: float = 0.0

        probe_done: bool = False
        probe_lock = threading.Lock()

        init_downloaded: bool      = False
        last_fresh_segs: List[Dict] = []

        def _fetch_key(key_url: str) -> bytes:
            if key_url in key_cache:
                return key_cache[key_url]
            
            with create_client(headers=all_headers, timeout=REQUEST_TIMEOUT, follow_redirects=True) as c:
                r = c.get(key_url)
                r.raise_for_status()
                key_data = r.content

            if len(key_data) != 16:
                logger.warning(f"AES-128 key length {len(key_data)} bytes (expected 16) for {key_url}")
            key_cache[key_url] = key_data
            return key_data

        def _decrypt_segment(fp: Path, seg_meta: Dict) -> None:
            """Decrypt a single AES-128 segment in-place (atomic replace)."""
            enc = seg_meta.get("enc") or {}
            if str(enc.get("method") or "NONE").upper() != "AES-128":
                return
            
            key_url = enc.get("key_url")
            if not key_url:
                raise RuntimeError(f"Missing AES-128 key URL for {fp.name}")
            
            key_data   = _fetch_key(key_url)
            seg_number = int(seg_meta.get("number", 0))
            logger.debug(f'AES-128 live-decrypt seg={fp.name} iv={enc.get("iv")}')

            decrypted = decrypt_aes128(fp.read_bytes(), key_data, enc.get("iv"), seg_number)
            tmp = fp.with_suffix(fp.suffix + ".dec")
            tmp.write_bytes(decrypted)

            for attempt in range(1, 6):
                try:
                    if fp.exists():
                        fp.unlink()
                    tmp.replace(fp)
                    break
                except OSError:
                    if attempt >= 5:
                        raise
                    time.sleep(0.05 * attempt)
            logger.debug(f"AES-128 live-decrypted -> {fp.name}")

        def _probe_first(fp: Path) -> None:
            nonlocal probe_done
            if probe_done:
                return
            
            with probe_lock:
                if probe_done:
                    return
                
                if fp.exists() and fp.stat().st_size > 0:
                    logger.info(f"Live HLS probe -> {fp.name}")
                    self._probe_media_file(fp)
                    probe_done = True

        def _process_batch(dl_batch: List[Dict], batch_paths: List[Path]) -> int:
            """
            Decrypt, probe, and accumulate segments into all_paths.
            Returns total bytes successfully processed in this batch.
            """
            nonlocal seg_done, total_bytes, elapsed_dur
            batch_bytes = 0

            for fp, seg_meta in zip(batch_paths, dl_batch):
                if not fp.exists() or fp.stat().st_size == 0:
                    logger.warning(f"Live HLS: empty or missing segment {fp.name}")
                    continue
                try:
                    _decrypt_segment(fp, seg_meta)
                except Exception as exc:
                    logger.error(f"Live HLS decrypt error for {fp.name}: {exc}")

                _probe_first(fp)
                sz           = fp.stat().st_size if fp.exists() else 0
                batch_bytes += sz
                total_bytes += sz
                elapsed_dur += seg_meta.get("duration", 0.0)
                all_paths.append(fp)
                seg_done    += 1

            return batch_bytes

        if base_url is None:
            base_url = hls_base_url(playlist_url)

        current_content: Optional[str] = first_content
        target_duration: int = 6
        empty_polls: int = 0
        poll_failures: int = 0

        _emit_live_progress(bar_manager, task_key, seg_done, total_bytes, 0.0)
        logger.info(f"Live HLS download started | url={playlist_url}")

        while not self._stop_check():
            if current_content is None:
                try:
                    with create_client(headers=all_headers, timeout=REQUEST_TIMEOUT, follow_redirects=True) as c:
                        resp = c.get(playlist_url)
                        resp.raise_for_status()
                        current_content = resp.text
                    poll_failures = 0
                except Exception as exc:
                    # When a live stream ends, its playlist URL typically starts returning errors (403/404 — token/session gone). 
                    poll_failures += 1
                    logger.error(f"Live HLS poll failed (failures={poll_failures}/{MAX_EMPTY_POLLS}): {exc}")
                    if poll_failures >= MAX_EMPTY_POLLS:
                        logger.info(f"Live HLS: {MAX_EMPTY_POLLS} consecutive poll failures — stream appears ended, stopping")
                        break
                    _sleep_interruptible(target_duration, self._stop_check)
                    continue

            new_segs, new_init_url, target_duration, media_sequence, is_ended = \
                parse_hls_live_playlist(current_content, base_url)

            # ── Init segment: download exactly once as seg_00000 ──────────────
            if new_init_url and not init_downloaded:
                logger.info(f"Live HLS: downloading init segment {new_init_url}")
                init_batch = [{
                    "url":      new_init_url,
                    "number":   0,
                    "seg_type": "init",
                    "enc":      {"method": "NONE"},
                }]

                init_paths = self._live_download_batch(init_batch, stream_dir, all_headers, stream)
                if init_paths and init_paths[0].exists() and init_paths[0].stat().st_size > 0:
                    all_paths.append(init_paths[0])
                    logger.info(f"Live HLS: init segment ready ({init_paths[0].name})")
                else:
                    logger.warning("Live HLS: init segment download produced no valid file")
                # seg_index stays at 1 — media segments start from seg_00001
                init_downloaded = True

            # ── Filter already-seen segments
            fresh_segs: List[Dict] = []
            for offset, seg in enumerate(new_segs):
                dedup_key = (media_sequence + offset, seg["url"])
                if dedup_key not in seen_seg_keys:
                    seen_seg_keys.add(dedup_key)
                    fresh_segs.append(seg)

            # ── Download & process fresh segments
            if fresh_segs:
                empty_polls = 0
                last_fresh_segs = fresh_segs

                # Check limit before building the batch
                if self.max_segments and seg_done >= self.max_segments:
                    logger.info(f"Live HLS: max_segments={self.max_segments} reached — stopping")
                    break

                # Clamp to remaining quota
                if self.max_segments:
                    remaining  = self.max_segments - seg_done
                    fresh_segs = fresh_segs[:remaining]

                # Assign stable global numbers to every segment in this batch.
                dl_batch: List[Dict] = []
                for seg in fresh_segs:
                    dl_batch.append({
                        "url":      seg["url"],
                        "number":   seg_index,
                        "seg_type": "media",
                        "enc":      seg.get("enc") or {"method": "NONE"},
                        "duration": seg.get("duration", 0.0),
                    })
                    seg_index += 1

                _batch_avg_dur = sum(s.get("duration", 0.0) for s in dl_batch) / max(len(dl_batch), 1)
                _segs_before = seg_done
                _bytes_before = total_bytes
                _elapsed_before = elapsed_dur

                def _batch_progress_cb(done, total_, cur_bytes, speed_bps, speed_label=None):
                    _emit_live_progress(
                        bar_manager, task_key,
                        _segs_before + done,
                        _bytes_before + cur_bytes,
                        speed_bps,
                        _elapsed_before + done * _batch_avg_dur,
                    )

                batch_t0    = time.monotonic()

                # _live_download_batch returns paths ordered by seg number
                batch_paths = self._live_download_batch(dl_batch, stream_dir, all_headers, stream, progress_cb=_batch_progress_cb)
                elapsed     = max(time.monotonic() - batch_t0, 0.001)

                batch_bytes = _process_batch(dl_batch, batch_paths)
                speed_bps   = batch_bytes / elapsed

                _emit_live_progress(bar_manager, task_key, seg_done, total_bytes, speed_bps, elapsed_dur)
                logger.info(f"Live HLS: +{len(fresh_segs)} segs | total={seg_done} | {_fmt_size(total_bytes)} | {speed_bps / 1024:.1f} KB/s | dur={_fmt_dur(elapsed_dur)}")

                if self.max_segments and seg_done >= self.max_segments:
                    logger.info(f"Live HLS: max_segments={self.max_segments} reached — stopping")
                    break

                if self.max_time and elapsed_dur >= self.max_time:
                    logger.info(f"Live HLS: max_time={self.max_time:.0f}s reached ({_fmt_dur(elapsed_dur)}) — stopping")
                    break

            if not fresh_segs:
                empty_polls += 1
                logger.info(f"Live HLS: no new segments (empty_polls={empty_polls}/{MAX_EMPTY_POLLS})")
                if empty_polls >= MAX_EMPTY_POLLS:
                    logger.info(f"Live HLS: {MAX_EMPTY_POLLS} consecutive polls with no new segments — stream appears ended, stopping")
                    break

            if is_ended:
                logger.info("Live HLS stream finished (#EXT-X-ENDLIST)")
                break

            if self._stop_check():
                logger.info("Live HLS: stop requested — exiting poll loop")
                break

            _sleep_interruptible(max(1, target_duration // 2), self._stop_check)
            current_content = None

        # ── Merge ─────────────────────────────────────────────────────────────
        if not all_paths:
            logger.error("Live HLS: no segments were downloaded — nothing to merge")
            return

        sample_url = last_fresh_segs[0]["url"] if last_fresh_segs else ""
        ext = detect_seg_ext(sample_url, default="ts")
        if ext == "m4s":
            ext = "mp4"

        out_path   = self.output_dir / self._out_filename(stream, ext)
        merge_size = sum(p.stat().st_size for p in all_paths if p.exists())

        logger.info(f"Live HLS binary merge | segs={len(all_paths)} | {_fmt_size(merge_size)} -> {out_path.name}")
        _emit_merge_progress(bar_manager, task_key, seg_done, merge_size)
        binary_merge_segments(all_paths, out_path, merge_logger=logger)

        if out_path.exists() and out_path.stat().st_size > 0:
            logger.info(f"Live HLS merge complete | segs={len(all_paths)} -> {out_path.name} ({_fmt_size(out_path.stat().st_size)})")
        else:
            logger.error(f"Live HLS binary merge produced an empty file: {out_path}")

    def _download_dash_live_stream(self, stream, bar_manager, live_decryption: bool = False, *, mpd_url: str, headers: Dict) -> None:
        """
        Download a live DASH stream by polling the dynamic MPD and downloading each new batch of segments as they appear.
        """
        task_key:   str  = self._stream_task_key(stream)
        stream_dir: Path = self._make_stream_dir(stream, "dash")

        seen_urls:       Set[str]    = set()
        all_paths:       List[Path]  = []
        seg_index:       int         = 0
        seg_done:        int         = 0
        total_bytes:     int         = 0
        last_media_url:  str         = ""
        elapsed_dur:     float       = 0.0
        init_downloaded: bool        = False
        init_path:       Optional[Path] = None
        probe_done:      bool        = False
        probe_lock                   = threading.Lock()
        min_update_period: float     = 4.0

        _decryptor = None
        if live_decryption and self.key:
            try:
                _decryptor = Decryptor()
                logger.info(f"Live DASH: CENC live-decrypt enabled for {stream.type}")
            except Exception as exc:
                logger.warning(f"Live DASH: Decryptor unavailable — segments will remain encrypted: {exc}")

        def _probe_first(fp: Path) -> None:
            nonlocal probe_done
            if probe_done:
                return
            with probe_lock:
                if probe_done:
                    return
                if fp.exists() and fp.stat().st_size > 0:
                    logger.info(f"Live DASH probe -> {fp.name}")
                    self._probe_media_file(fp)
                    probe_done = True

        def _decrypt_seg(fp: Path, is_init: bool = False) -> bool:
            """Returns True if segment is usable (decrypted or no-decrypt-needed), False if failed."""
            nonlocal init_path
            if is_init:
                init_path = fp
                logger.info(f"Live DASH: init segment cached -> {fp.name}")
                return True

            if not _decryptor or not self.key:
                return True

            dec_tmp = fp.with_suffix(fp.suffix + ".dec")
            try:
                ok, message, _data = _decryptor.decrypt_segment_live(
                    encrypted_path=str(fp),
                    decrypted_path=str(dec_tmp),
                    raw_keys=self.key,
                    init_path=str(init_path) if init_path and init_path.exists() else None,
                )

                if not ok:
                    logger.debug(f"Live DASH: decrypt failed for {fp.name}: {message}")
                    dec_tmp.unlink(missing_ok=True)
                    return False
                
                if not dec_tmp.exists():
                    logger.error(f"Live DASH: decrypt produced no output for {fp.name}")
                    return False

                for attempt in range(1, 6):
                    try:
                        fp.unlink(missing_ok=True)
                        dec_tmp.replace(fp)
                        break
                    except OSError:
                        if attempt >= 5:
                            raise
                        time.sleep(0.05 * attempt)
                logger.debug(f"Live DASH: CENC decrypted -> {fp.name}")
                return True

            except Exception as exc:
                logger.error(f"Live DASH: decrypt error for {fp.name}: {exc}")
                dec_tmp.unlink(missing_ok=True)
                return False

        def _download_batch(dl_batch: List[Dict], progress_cb=None) -> List[Path]:
            """
            Download a DASH batch and return paths ordered by segment number
            """
            self._run_dl(
                dl_batch,
                stream_dir,
                headers,
                progress_cb=progress_cb,
                stream=stream,
                event_cb=None,
            )
            ordered: List[Path] = []
            for seg in dl_batch:
                seg_ext = detect_seg_ext(seg.get("url", ""), default="mp4")
                if seg_ext == "m4s":
                    seg_ext = "mp4"

                ordered.append(stream_dir / f"seg_{seg['number']:05d}.{seg_ext}")
            return ordered

        def _fetch_and_parse_mpd() -> Tuple[Optional[List], float, bool]:
            nonlocal min_update_period
            try:
                parser = DashParser(mpd_url, headers=headers)
                if not parser.fetch_manifest():
                    return None, min_update_period, False
                
                streams    = parser.parse_streams()
                meta       = parser.live_meta
                upd_period = meta["min_update_period"]
                is_static  = meta["is_ended"]

                for s in streams:
                    if s.type == stream.type and s.id == stream.id:
                        return s.segments, upd_period, is_static
                
                candidates = [s for s in streams if s.type == stream.type]
                if candidates:
                    best = min(candidates, key=lambda s: abs((s.bitrate or 0) - (stream.bitrate or 0)))
                    logger.debug(f"Live DASH: id mismatch — falling back to bitrate match id={best.id} bw={best.bitrate}")
                    return best.segments, upd_period, is_static
                
                logger.warning("Live DASH: no matching representation in refreshed MPD")
                return None, upd_period, is_static
            
            except Exception as exc:
                logger.error(f"Live DASH: MPD fetch/parse failed: {exc}")
                return None, min_update_period, False

        current_segments = list(stream.segments)
        is_ended = False
        empty_polls: int = 0
        poll_failures: int = 0

        _emit_live_progress(bar_manager, task_key, seg_done, total_bytes, 0.0, 0.0)
        logger.info(f"Live DASH download started | stream={stream} | url={mpd_url}")

        while not self._stop_check():
            if current_segments is None:
                segs, min_update_period, is_ended = _fetch_and_parse_mpd()
                if segs is None:
                    # A sustained run of failed MPD polls (e.g. the manifest URL starts returning 403/404 once the event ends). 
                    poll_failures += 1
                    logger.warning(f"Live DASH: empty MPD poll (failures={poll_failures}/{MAX_EMPTY_POLLS}) — backing off {min_update_period:.0f} s")
                    if poll_failures >= MAX_EMPTY_POLLS:
                        logger.info(f"Live DASH: {MAX_EMPTY_POLLS} consecutive poll failures — stream appears ended, stopping")
                        break
                    _sleep_interruptible(min_update_period or 4.0, self._stop_check)
                    current_segments = None
                    continue

                poll_failures = 0
                current_segments = segs

            init_segs  = [s for s in current_segments if s.seg_type == "init"]
            media_segs = [s for s in current_segments if s.seg_type == "media"]

            if init_segs and not init_downloaded:
                init_seg = init_segs[0]
                if init_seg.url not in seen_urls:
                    logger.info(f"Live DASH: downloading init segment | url={init_seg.url}")
                    init_entry: Dict = {
                        "url":      init_seg.url,
                        "number":   0,
                        "seg_type": "init",
                        "enc":      {"method": "NONE"},
                    }

                    if init_seg.byte_range:
                        init_entry["headers"] = {"Range": f"bytes={init_seg.byte_range}"}

                    init_paths = _download_batch([init_entry])
                    if init_paths:
                        _decrypt_seg(init_paths[0], is_init=True)
                        all_paths.extend(init_paths)
                        seg_index = 1
                        logger.info(f"Live DASH: init segment ready | {init_paths[0].name}")

                    seen_urls.add(init_seg.url)
                init_downloaded = True

            fresh_segs: List = []
            for seg in media_segs:
                if seg.url not in seen_urls:
                    seen_urls.add(seg.url)
                    fresh_segs.append(seg)
                    last_media_url = seg.url

            if fresh_segs:
                empty_polls = 0
                if self.max_segments:
                    remaining = self.max_segments - seg_done
                    if remaining <= 0:
                        break
                    fresh_segs = fresh_segs[:remaining]

                # Clamp the batch to the remaining max_time budget so we don't
                # grab the entire DVR back-window in one shot.
                # Keep the MOST RECENT segments (tail, nearest the live edge)
                if self.max_time and elapsed_dur < self.max_time:
                    budget = self.max_time - elapsed_dur
                    clamped: List = []
                    acc = 0.0
                    for s in reversed(fresh_segs):
                        clamped.append(s)
                        acc += getattr(s, "duration", 0.0)
                        if acc >= budget:
                            break
                    clamped.reverse()
                    if len(clamped) < len(fresh_segs):
                        logger.info(f"Live DASH: clamping batch to most recent {len(clamped)}/{len(fresh_segs)} segs (~{acc:.0f}s) for max_time budget {budget:.0f}s")
                    fresh_segs = clamped

                dl_batch: List[Dict] = []
                for seg in fresh_segs:
                    entry: Dict = {
                        "url":      seg.url,
                        "number":   seg_index,
                        "seg_type": "media",
                        "enc":      {"method": "NONE"},
                    }
                    if seg.byte_range:
                        entry["headers"] = {"Range": f"bytes={seg.byte_range}"}
                    dl_batch.append(entry)
                    seg_index += 1

                _batch_avg_dur  = sum(getattr(s, "duration", 0.0) for s in fresh_segs) / max(len(fresh_segs), 1)
                _segs_before    = seg_done
                _bytes_before   = total_bytes
                _elapsed_before = elapsed_dur

                def _batch_progress_cb(done, total_, cur_bytes, speed_bps, speed_label=None):
                    _emit_live_progress(
                        bar_manager, task_key,
                        _segs_before + done,
                        _bytes_before + cur_bytes,
                        speed_bps,
                        _elapsed_before + done * _batch_avg_dur,
                    )

                logger.debug(f"Live DASH: batch first URL: {getattr(fresh_segs[0], 'url', '?')[:120]}")
                batch_t0 = time.monotonic()
                batch_paths = _download_batch(dl_batch, progress_cb=_batch_progress_cb)
                elapsed = max(time.monotonic() - batch_t0, 0.001)
                batch_bytes = 0
                decrypt_fails = 0
                raw_bytes = 0

                for fp in batch_paths:
                    if not fp.exists() or fp.stat().st_size == 0:
                        logger.warning(f"Live DASH: empty or missing segment {fp.name}")
                        continue

                    raw_bytes += fp.stat().st_size
                    ok = _decrypt_seg(fp, is_init=False)
                    if not ok:
                        decrypt_fails += 1
                        continue

                    _probe_first(fp)
                    sz           = fp.stat().st_size if fp.exists() else 0
                    batch_bytes += sz
                    total_bytes += sz
                    all_paths.append(fp)
                    seg_done    += 1

                if decrypt_fails:
                    avg_raw = raw_bytes / max(decrypt_fails, 1)
                    # Keys are already validated before download; tiny uniform segments
                    hint = "CDN served stub/error pages — live event may have ended or token expired" if avg_raw < 16 * 1024 else "segments may be corrupt or keys mismatched"
                    logger.warning(f"Live DASH: {decrypt_fails}/{len(batch_paths)} segments failed decrypt (avg {_fmt_size(int(avg_raw))}/seg — {hint})")

                speed_bps    = batch_bytes / elapsed
                elapsed_dur += sum(getattr(seg, "duration", 0.0) for seg in fresh_segs)
                _emit_live_progress(bar_manager, task_key, seg_done, total_bytes, speed_bps, elapsed_dur)
                logger.info(f"Live DASH: +{len(fresh_segs)} segs | total={seg_done} | {_fmt_size(total_bytes)} | {speed_bps / 1024:.1f} KB/s | dur={_fmt_dur(elapsed_dur)}")

                if self.max_segments and seg_done >= self.max_segments:
                    logger.info(f"Live DASH: max_segments={self.max_segments} reached — stopping")
                    break

                if self.max_time:
                    logger.debug(f"Live DASH: max_time check elapsed={elapsed_dur:.1f}s limit={self.max_time:.0f}s")
                    if elapsed_dur >= self.max_time:
                        logger.info(f"Live DASH: max_time={self.max_time:.0f}s reached ({_fmt_dur(elapsed_dur)}) — stopping")
                        break

            if not fresh_segs:
                empty_polls += 1
                logger.info(f"Live DASH: no new segments (empty_polls={empty_polls}/{MAX_EMPTY_POLLS})")
                if empty_polls >= MAX_EMPTY_POLLS:
                    logger.info(f"Live DASH: {MAX_EMPTY_POLLS} consecutive polls with no new segments — stream appears ended, stopping")
                    break

            if is_ended:
                logger.info("Live DASH: MPD type=static — stream finished")
                break

            if self._stop_check():
                logger.info("Live DASH: stop requested — exiting poll loop")
                break

            poll_sleep = max(1.0, min_update_period / 2.0)
            logger.debug(f"Live DASH: sleeping {poll_sleep:.1f} s before next poll")
            _sleep_interruptible(poll_sleep, self._stop_check)
            current_segments = None

        if not all_paths:
            logger.error("Live DASH: no segments were downloaded — nothing to merge")
            return

        if seg_done == 0:
            logger.error("Live DASH: every media segment failed — no usable media, skipping merge. The CDN likely served stub/error responses (live event ended, DVR window expired, or geo/token rejected). Re-run while the event is actually live.")
            console.print("[red]Live DASH: no usable media — the stream's segments were rejected by the CDN (event ended / token expired). Re-run while the event is live.")
            return

        ext = detect_seg_ext(last_media_url, default="mp4")
        if ext == "m4s":
            ext = "mp4"

        out_path   = self.output_dir / self._out_filename(stream, ext)
        merge_size = sum(p.stat().st_size for p in all_paths if p.exists())

        logger.info(f"Live DASH binary merge | segs={len(all_paths)} | {_fmt_size(merge_size)} -> {out_path.name}")
        _emit_merge_progress(bar_manager, task_key, seg_done, merge_size)
        binary_merge_segments(all_paths, out_path, merge_logger=logger)

        if out_path.exists() and out_path.stat().st_size > 0:
            logger.info(f"Live DASH merge complete | segs={len(all_paths)} -> {out_path.name} ({_fmt_size(out_path.stat().st_size)})")
        else:
            logger.error(f"Live DASH binary merge produced an empty file: {out_path}")