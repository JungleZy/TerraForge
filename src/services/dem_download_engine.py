"""
DEM download engine for datasets hosted behind Earthdata Login (URS).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

import aiofiles
import aiohttp

from src.core.config import Config
from src.services.config_manager import ConfigManager
from src.services.earthdata_client import EarthdataAuthError, EarthdataClient
from src.services.proxy_autodetect import resolve_from_config

logger = logging.getLogger(__name__)


def _redact_url_query(text: str) -> str:
    """剥掉消息里 URL 的 query —— 签名 URL 的凭据/签名不能进日志或 DB。"""
    return re.sub(r"\?[^\s\"')]*", "?<redacted>", str(text))


async def _report_progress(
    progress_callback: Optional[Callable[[str, str, Optional[str], Optional[int]], Any]],
    granule: str,
    status: str,
    error: Optional[str],
    size_bytes: Optional[int],
) -> None:
    """回调只负责进度/记账，其异常不外抛：progress 回调（sqlite 写库 +
    emit）若在下载 try 块内抛出，会被下载重试的 except Exception
    当成下载失败（白下 30-50MB），最终 failed 回调再抛还会击穿无
    return_exceptions 的 gather、取消其余 granule 协程。DB 层瞬时故障只记日志。"""
    if progress_callback is None:
        return
    try:
        await progress_callback(granule, status, error, size_bytes)
    except Exception as e:
        logger.warning(f"DEM progress callback failed ({granule}, {status}): {e}")


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
    def _client_timeout(request_timeout: int) -> "aiohttp.ClientTimeout":
        # NO total cap: DEM tiles are 30-50MB COGs; over a slow/proxied link a
        # total timeout kills the transfer mid-stream (leaving .part files).
        # Use stall timeouts instead — abort only if connect or a read stalls.
        return aiohttp.ClientTimeout(total=None, sock_connect=request_timeout, sock_read=request_timeout)

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
        # 生效代理：手动 proxy_url > 自动探测（见 services/proxy_autodetect）。
        # to_thread 见 download_engine 同处注释：解析可能阻塞等后台探测。
        proxy_url = await asyncio.to_thread(resolve_from_config, self.config)

        username = self.config.get("earthdata_username", "") or ""
        password = self.config.get("earthdata_password", "") or ""

        dem_cache_enabled = (self.config.get("dem_cache_enabled", "true") or "true").lower() == "true"
        cache_dir: Optional[Path] = Path(Config.CACHE_DIR) / "dem" if dem_cache_enabled else None

        earth = EarthdataClient(username=username, password=password, proxy_url=proxy_url)
        base_url = self._dataset_base_url(dataset)
        requires_auth = self._dataset_requires_auth(dataset)

        timeout = self._client_timeout(request_timeout)
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
                        await _report_progress(progress_callback, granule, "completed", None, dest.stat().st_size)
                        return

                    if self._try_promote_from_cache(local_name, dest, cache_dir):
                        logger.info(f"DEM cache hit: {local_name} (promoted from {cache_dir})")
                        await _report_progress(progress_callback, granule, "completed", None, dest.stat().st_size)
                        return

                    file_url = base_url + granule
                    # 签名 URL 只解析一次、重试复用：签名约 1 小时有效而重试退避
                    # 是秒级，每次 attempt 重签只是白走 URS/跳转往返。下载遇 403
                    # （签名过期）时清空 signed_url，下个 attempt 循环内重签。
                    signed_url: Optional[str] = None
                    last_err: Optional[str] = None
                    for attempt in range(max_retries + 1):
                        if stop_flag and stop_flag.is_set():
                            # C4: 暂停不是失败 —— 回写 pending，恢复时重新下载，
                            # 不能留下 downloading 孤儿。
                            await _report_progress(progress_callback, granule, "pending", None, None)
                            return

                        try:
                            # downloading 只在首次 attempt 上报：重试重报只是
                            # 无意义的状态翻转，dem_files 行和前端都不需要。
                            if attempt == 0:
                                await _report_progress(progress_callback, granule, "downloading", None, None)

                            if requires_auth:
                                if signed_url is None:
                                    signed_url = await earth.get_signed_url(session=session, file_url=file_url)
                                get_url = signed_url
                            else:
                                get_url = file_url
                            async with session.get(get_url, proxy=proxy_url or None) as resp:
                                if resp.status == 404:
                                    # I12: 无数据颗粒（海洋/覆盖范围外）—— 标记 skipped，
                                    # 不重试、不计 failed、不阻断任务完成（部分成功语义）。
                                    await _report_progress(
                                        progress_callback,
                                        granule, "skipped", "no data at this location (HTTP 404)", None,
                                    )
                                    return
                                if resp.status == 403 and requires_auth:
                                    # 签名 URL 过期：重签即可恢复，不按普通网络
                                    # 错误白白耗尽整轮指数退避。
                                    signed_url = None
                                    raise RuntimeError("Download HTTP 403 (signed URL expired, will re-sign)")
                                if resp.status != 200:
                                    raise RuntimeError(f"Download HTTP {resp.status}")

                                try:
                                    content_length = resp.headers.get("Content-Length")
                                    expected_size = int(content_length) if content_length is not None else None
                                except (TypeError, ValueError):
                                    expected_size = None

                                tmp = dest.with_suffix(dest.suffix + ".part")
                                size = 0
                                async with aiofiles.open(tmp, "wb") as f:
                                    async for chunk in resp.content.iter_chunked(1024 * 256):
                                        if stop_flag and stop_flag.is_set():
                                            raise RuntimeError("stopped")
                                        await f.write(chunk)
                                        size += len(chunk)

                                # I13: 完整性校验 —— 截断文件判失败、不落盘、不写缓存
                                # （否则 size>0 的半成品会永久污染全局缓存）。
                                if expected_size is not None and size != expected_size:
                                    raise RuntimeError(
                                        f"truncated download: got {size} bytes, expected {expected_size}"
                                    )

                                tmp.replace(dest)

                            self._save_to_cache(dest, local_name, cache_dir)

                            await _report_progress(progress_callback, granule, "completed", None, dest.stat().st_size)
                            return
                        except EarthdataAuthError as e:
                            # 401/缺凭据不可重试：坏凭据下 N 颗粒 × M 重试 × 3 跳
                            # 全是必败请求。直接判该颗粒失败、不进指数退避；不
                            # 终止整个任务 —— 同任务其余颗粒会在各自首次签名时
                            # 同样快速失败，用户改凭据后恢复任务即可重下。
                            await _report_progress(
                                progress_callback, granule, "failed", _redact_url_query(str(e)), None,
                            )
                            return
                        except Exception as e:
                            # Remove the partial .part so failed/interrupted attempts
                            # don't leave litter (and a later run re-downloads cleanly).
                            part = dest.with_suffix(dest.suffix + ".part")
                            try:
                                if part.exists():
                                    part.unlink()
                            except OSError:
                                pass
                            if stop_flag and stop_flag.is_set():
                                # C4: 下载途中暂停 —— 回写 pending，恢复时重新下载。
                                await _report_progress(progress_callback, granule, "pending", None, None)
                                return
                            last_err = _redact_url_query(str(e))
                            logger.warning(f"DEM download failed ({granule}) attempt {attempt+1}/{max_retries+1}: {last_err}")
                            # 分段退避：每 0.5s 检查一次 stop —— 一觉睡到 10s 的话
                            # 暂停/停止要等 sleep 结束才生效。
                            delay = min(2 ** attempt, 10)
                            elapsed = 0.0
                            while elapsed < delay:
                                if stop_flag and stop_flag.is_set():
                                    break
                                step = min(0.5, delay - elapsed)
                                await asyncio.sleep(step)
                                elapsed += step

                    await _report_progress(progress_callback, granule, "failed", last_err, None)

            await asyncio.gather(*(one(g) for g in granules))

