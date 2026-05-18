"""
DEM download engine for datasets hosted behind Earthdata Login (URS).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

import aiofiles
import aiohttp

from services.config_manager import ConfigManager
from services.earthdata_client import EarthdataClient

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str, str, Optional[str], Optional[int]], "asyncio.Future[Any]"]


class DemDownloadEngine:
    def __init__(self):
        self.config = ConfigManager()

    def _dataset_base_url(self, dataset: str) -> str:
        # For now only ASTGTM.003 is supported.
        if dataset != "ASTGTM.003":
            raise ValueError(f"Unsupported DEM dataset: {dataset}")
        return "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/ASTGTM.003/"

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

        earth = EarthdataClient(username=username, password=password, proxy_url=proxy_url)
        base_url = self._dataset_base_url(dataset)

        timeout = aiohttp.ClientTimeout(total=request_timeout)
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=concurrent_downloads)
        jar = aiohttp.CookieJar(unsafe=True)

        semaphore = asyncio.Semaphore(concurrent_downloads)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector, cookie_jar=jar) as session:
            async def one(granule: str):
                async with semaphore:
                    if stop_flag and stop_flag.is_set():
                        return

                    dest = output_dir / granule
                    if dest.exists() and dest.stat().st_size > 0:
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

                            signed_url = await earth.get_signed_url(session=session, file_url=file_url)
                            async with session.get(signed_url, proxy=proxy_url or None) as resp:
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

