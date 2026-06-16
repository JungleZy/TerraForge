"""
DEM download engine for datasets hosted behind Earthdata Login (URS).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

import aiofiles
import aiohttp

from config import Config
from services.config_manager import ConfigManager
from services.earthdata_client import EarthdataClient

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str, str, Optional[str], Optional[int]], "asyncio.Future[Any]"]


class DemDownloadEngine:
    def __init__(self):
        self.config = ConfigManager()

    def _dataset_base_url(self, dataset: str) -> str:
        # LP DAAC cloud Data Pool (Earthdata-protected): ASTGTM.003 = elevation,
        # ASTWBD.001 = water. COP-DEM-GLO-30 = Copernicus DEM on a public AWS bucket.
        lpdaac = "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
        if dataset in ("ASTGTM.003", "ASTWBD.001"):
            return f"{lpdaac}{dataset}/"
        if dataset == "COP-DEM-GLO-30":
            return "https://copernicus-dem-30m.s3.amazonaws.com/"
        raise ValueError(f"Unsupported DEM dataset: {dataset}")

    @staticmethod
    def _dataset_requires_auth(dataset: str) -> bool:
        # Copernicus GLO-30 is a public AWS Open Data bucket (no signing);
        # LP DAAC products require an Earthdata Login signed URL.
        return dataset != "COP-DEM-GLO-30"

    @staticmethod
    def _link_or_copy(src: Path, dst: Path) -> None:
        # Hard link first (zero-copy, instant). Fall back to a same-directory
        # temp copy + atomic replace so other tasks never promote a half file.
        try:
            os.link(src, dst)
        except OSError:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f"{dst.name}.part.{os.getpid()}.{id(dst)}")
            try:
                shutil.copyfile(src, tmp)
                tmp.replace(dst)
            finally:
                if tmp.exists():
                    tmp.unlink()

    def _try_promote_from_cache(self, granule: str, dest: Path, cache_dir: Optional[Path]) -> bool:
        """Promote a cached granule into the task's output dir.

        Returns True iff dest now exists with the cached content.
        """
        if cache_dir is None:
            return False
        cached = cache_dir / granule
        if not (cached.exists() and cached.stat().st_size > 0):
            return False
        try:
            if dest.exists():
                dest.unlink()
            self._link_or_copy(cached, dest)
            return True
        except Exception as e:
            logger.warning(f"DEM cache promotion failed for {granule}: {e}")
            return False

    def _save_to_cache(self, src: Path, granule: str, cache_dir: Optional[Path]) -> None:
        """Best-effort: mirror a freshly downloaded granule into the cache."""
        if cache_dir is None:
            return
        cached = cache_dir / granule
        if cached.exists():
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._link_or_copy(src, cached)
        except Exception as e:
            logger.warning(f"DEM cache save failed for {granule}: {e}")

    async def download_files(
        self,
        dataset: str,
        granules: List[str],
        output_dir: Path,
        progress_callback: Optional[Callable[[str, str, Optional[str], Optional[int]], Any]] = None,
        stop_flag: Optional[asyncio.Event] = None,
    ) -> None:
        """
        Download a list of granule filenames to output_dir.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        concurrent_downloads = int(self.config.get("concurrent_downloads", "5"))
        request_timeout = int(self.config.get("request_timeout", "60"))
        max_retries = int(self.config.get("max_retries", "3"))
        proxy_url = self.config.get("proxy_url", "") or ""

        username = self.config.get("earthdata_username", "") or ""
        password = self.config.get("earthdata_password", "") or ""

        dem_cache_enabled = (self.config.get("dem_cache_enabled", "true") or "true").lower() == "true"
        cache_dir: Optional[Path] = Path(Config.CACHE_DIR) / "dem" if dem_cache_enabled else None

        earth = EarthdataClient(username=username, password=password, proxy_url=proxy_url)
        base_url = self._dataset_base_url(dataset)
        requires_auth = self._dataset_requires_auth(dataset)

        timeout = aiohttp.ClientTimeout(total=request_timeout)
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=concurrent_downloads)
        jar = aiohttp.CookieJar(unsafe=True)

        semaphore = asyncio.Semaphore(concurrent_downloads)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector, cookie_jar=jar, trust_env=True) as session:
            async def one(granule: str):
                async with semaphore:
                    if stop_flag and stop_flag.is_set():
                        return

                    # Remote granule may be a nested path (Copernicus); the local
                    # file and cache key use the flat basename so list_dem_tifs finds it.
                    local_name = Path(granule).name
                    dest = output_dir / local_name
                    if dest.exists() and dest.stat().st_size > 0:
                        if progress_callback:
                            await progress_callback(granule, "completed", None, dest.stat().st_size)
                        return

                    if self._try_promote_from_cache(local_name, dest, cache_dir):
                        logger.info(f"DEM cache hit: {local_name} (promoted from {cache_dir})")
                        if progress_callback:
                            await progress_callback(granule, "completed", None, dest.stat().st_size)
                        return

                    file_url = base_url + granule
                    last_err: Optional[str] = None
                    for attempt in range(max_retries + 1):
                        if stop_flag and stop_flag.is_set():
                            return

                        try:
                            if progress_callback:
                                await progress_callback(granule, "downloading", None, None)

                            if requires_auth:
                                get_url = await earth.get_signed_url(session=session, file_url=file_url)
                            else:
                                get_url = file_url
                            async with session.get(get_url, proxy=proxy_url or None) as resp:
                                if resp.status != 200:
                                    raise RuntimeError(f"Download HTTP {resp.status}")

                                tmp = dest.with_suffix(dest.suffix + ".part")
                                size = 0
                                async with aiofiles.open(tmp, "wb") as f:
                                    async for chunk in resp.content.iter_chunked(1024 * 256):
                                        if stop_flag and stop_flag.is_set():
                                            raise RuntimeError("stopped")
                                        await f.write(chunk)
                                        size += len(chunk)

                                tmp.replace(dest)

                            self._save_to_cache(dest, local_name, cache_dir)

                            if progress_callback:
                                await progress_callback(granule, "completed", None, dest.stat().st_size)
                            return
                        except Exception as e:
                            last_err = str(e)
                            logger.warning(f"DEM download failed ({granule}) attempt {attempt+1}/{max_retries+1}: {e}")
                            await asyncio.sleep(min(2 ** attempt, 10))

                    if progress_callback:
                        await progress_callback(granule, "failed", last_err, None)

            await asyncio.gather(*(one(g) for g in granules))

