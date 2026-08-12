"""
DEM download engine for datasets hosted behind Earthdata Login (URS).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

import aiofiles
import aiohttp

from src.core.config import Config
from src.services.config_manager import ConfigManager
from src.services.earthdata_client import EarthdataAuthError, EarthdataClient
from src.services.proxy_autodetect import resolve_from_config

logger = logging.getLogger(__name__)

# 在途字节回调的最小上报间隔（秒）。256KB 一块，千兆链路下一颗粒每秒上百块，
# 逐块回调只是把同一个数字重算上百遍；聚合到 0.25s 既够上层 1s 节流的 emit
# 用，也让 SpeedMeter 的样本密度保持在合理量级。
_BYTES_REPORT_MIN_INTERVAL = 0.25


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


async def _report_bytes(
    bytes_callback: Optional[Callable[[str, int], Any]],
    granule: str,
    n_bytes: int,
) -> None:
    """上报「刚收到 n_bytes 字节」。与 _report_progress 同一约定：异常只记日志。

    它在下载的 try 块里被 await，抛出会被重试的 except Exception 当成下载失败
    （白下几十 MB），最后一次还会击穿无 return_exceptions 的 gather。"""
    if bytes_callback is None:
        return
    try:
        await bytes_callback(granule, n_bytes)
    except Exception as e:
        logger.warning(f"DEM bytes callback failed ({granule}): {e}")


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

    async def download_files(
        self,
        dataset: str,
        granules: List[str],
        output_dir: Path,
        progress_callback: Optional[Callable[[str, str, Optional[str], Optional[int]], Any]] = None,
        bytes_callback: Optional[Callable[[str, int], Any]] = None,
        stop_flag: Optional[asyncio.Event] = None,
        max_concurrent: Optional[int] = None,
        disk_recheck=None,
    ) -> None:
        """
        Download a list of granule filenames to output_dir.

        progress_callback 报颗粒级状态迁移；bytes_callback 报在途网络字节
        （每 _BYTES_REPORT_MIN_INTERVAL 聚合一次），上层据此算下载速度。

        max_concurrent 是 ResourceScheduler **授予**的并发连接数。None = 直接读
        `concurrent_downloads` 配置，也就是改造前的行为（只留给直调与测试）。
        为什么不能让本方法自己读配置就算数：`concurrent_downloads` 出厂是 50，
        四条管线各起一个任务就是 200 条连接，没有任何全局上界 —— 那正是
        resource_scheduler 存在的理由。配置值现在的语义是「**请求**多少条」，
        管理器拿它去 reserve，真正开出来的连接数是这里收到的授予量
        （可能小于请求量，最低 1 条：一条也能跑完，只是慢）。

        disk_recheck 是 `disk_budget.RunningRecheck`（None = 不查，直调与测试的
        那一档）。**颗粒就是这条管线天然的批**：一颗 COG 是 30-50 MB，正好是
        复查要防的量级，所以每颗开下之前查一次 —— 逐 chunk 查是纯浪费（复查
        自己还有时间节流）。判死之后不抛：置内部标记，让在途与排队的颗粒各自
        沿**用户按暂停那条**收手路径退出（见 _stop_requested）。判决对象归调用
        方所有，收手之后调用方从 `disk_recheck.blocked` 取判决写终态。
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        if max_concurrent is None:
            concurrent_downloads = int(self.config.get("concurrent_downloads", "5"))
        else:
            # 授予量已由调度器保证 >= 1，这里的 max(1, ...) 只防调用方传 0/负数
            # —— Semaphore(0) 会让所有颗粒永久挂起，表现是任务卡在 0% 不动。
            concurrent_downloads = max(1, int(max_concurrent))
        request_timeout = int(self.config.get("request_timeout", "60"))
        max_retries = int(self.config.get("max_retries", "3"))
        # 生效代理：手动 proxy_url > 自动探测（见 services/proxy_autodetect）。
        # to_thread 见 download_engine 同处注释：解析可能阻塞等后台探测。
        proxy_url = await asyncio.to_thread(resolve_from_config, self.config)

        username = self.config.get("earthdata_username", "") or ""
        password = self.config.get("earthdata_password", "") or ""

        dem_cache_enabled = (self.config.get("dem_cache_enabled", "true") or "true").lower() == "true"
        cache_dir: Optional[Path] = Path(Config.CACHE_DIR) / "dem" if dem_cache_enabled else None
        if cache_dir is not None:
            # 提前建好：它同时是缓存落点**和**原子写临时件的落点（见下面的 tmp）。
            # 建不出来（只读介质 / 权限）就退化成「无缓存」，不能让整个任务失败。
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f"DEM cache dir unavailable ({e}); downloading without cache")
                cache_dir = None

        earth = EarthdataClient(username=username, password=password, proxy_url=proxy_url)
        base_url = self._dataset_base_url(dataset)
        requires_auth = self._dataset_requires_auth(dataset)

        timeout = self._client_timeout(request_timeout)
        connector = aiohttp.TCPConnector(limit=concurrent_downloads, limit_per_host=concurrent_downloads)
        jar = aiohttp.CookieJar(unsafe=True)

        semaphore = asyncio.Semaphore(concurrent_downloads)

        # 磁盘复查判死之后的内部停止状态。放在闭包里而不是 self 上：DemTaskManager
        # 全进程只有一个 engine 实例，挂在 self 上就是两个并发 DEM 任务互相覆盖
        # （A 的收手理由变成 B 的判决），而这个标记的职能是「决定要不要停掉一个
        # 任务」—— 认错任务的代价太大。
        budget_blocked: Dict[str, Any] = {'verdict': None}

        def _stop_requested() -> bool:
            """要不要收手：用户的停止标记，或磁盘复查判死。

            两者共用**完全同一条**收手路径（排队的直接退出、在途的中断后回写
            pending），因为对下游而言它们是同一件事：这一轮不再继续，已下好的
            颗粒留着，恢复时从断点接上。判死刻意不抛异常 —— 抛出去会被管理器
            的兜底 except 变成一句 RuntimeError，而用户需要看到的是判决里那
            四个数字（还差多少、腾多少）。
            """
            if stop_flag is not None and stop_flag.is_set():
                return True
            return budget_blocked['verdict'] is not None

        async with aiohttp.ClientSession(timeout=timeout, connector=connector, cookie_jar=jar, trust_env=True) as session:
            async def one(granule: str):
                async with semaphore:
                    if _stop_requested():
                        return

                    # 开下这一颗之前复查一次磁盘。位置在 semaphore 之内、
                    # dest.exists() 快速路径之前都无所谓（复查自带时间节流），
                    # 但必须在**真的开始写**之前 —— 这条闸门的全部价值就是抢在
                    # ENOSPC 之前：GTiff/COG 边写边落盘，写失败留下的是一份非空
                    # 半成品，而下一轮的断点判定是「存在且非空就跳过」。
                    if disk_recheck is not None and budget_blocked['verdict'] is None:
                        blocked = disk_recheck.blocking_verdict()
                        if blocked is not None:
                            budget_blocked['verdict'] = blocked
                            logger.error(
                                f"DEM download halted by the in-flight disk recheck: "
                                f"{blocked.reason}")
                            # 回写 pending 而不是 failed：这一颗一次都没被尝试过，
                            # 腾出空间后恢复任务就该重新排上（同暂停的口径）。
                            await _report_progress(progress_callback, granule, "pending", None, None)
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

                    # 原子写的临时件落在**缓存目录**（启用缓存时）：任务目录里只
                    # 出现最终产物 —— 用户看到的不再是一堆 `*.tif.part`；文件名带
                    # pid，启动清扫也能按归属回收残留（task_cleanup._PART_GLOB /
                    # _part_owner_pid 只扫 CACHE_DIR）。关掉缓存时没有缓存目录可
                    # 用，退回任务目录内写：强行走 CACHE_DIR 会让「不要缓存」的
                    # 用户凭空多一次跨盘整份拷贝。
                    tmp = (cache_dir or output_dir) / f"{local_name}.part.{os.getpid()}.{id(dest)}"

                    file_url = base_url + granule
                    # 签名 URL 只解析一次、重试复用：签名约 1 小时有效而重试退避
                    # 是秒级，每次 attempt 重签只是白走 URS/跳转往返。下载遇 403
                    # （签名过期）时清空 signed_url，下个 attempt 循环内重签。
                    signed_url: Optional[str] = None
                    last_err: Optional[str] = None
                    for attempt in range(max_retries + 1):
                        if _stop_requested():
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

                                size = 0
                                pending_bytes = 0
                                last_report = time.monotonic()
                                async with aiofiles.open(tmp, "wb") as f:
                                    async for chunk in resp.content.iter_chunked(1024 * 256):
                                        if _stop_requested():
                                            raise RuntimeError("stopped")
                                        await f.write(chunk)
                                        size += len(chunk)
                                        # 在途字节是速度显示的唯一数据源：单颗 COG
                                        # 30-50MB、几分钟起步，只在颗粒收尾报一次总
                                        # 大小的话，中间几分钟一发推送都没有，前端会
                                        # 判过期显示 0 B/s。
                                        if bytes_callback is not None:
                                            pending_bytes += len(chunk)
                                            now = time.monotonic()
                                            if now - last_report >= _BYTES_REPORT_MIN_INTERVAL:
                                                last_report = now
                                                await _report_bytes(bytes_callback, granule, pending_bytes)
                                                pending_bytes = 0
                                if pending_bytes:
                                    await _report_bytes(bytes_callback, granule, pending_bytes)

                                # I13: 完整性校验 —— 截断文件判失败、不落盘、不写缓存
                                # （否则 size>0 的半成品会永久污染全局缓存）。
                                if expected_size is not None and size != expected_size:
                                    raise RuntimeError(
                                        f"truncated download: got {size} bytes, expected {expected_size}"
                                    )

                            # 先原子落进缓存（同目录 rename），再从缓存 link/copy 进
                            # 任务目录 —— 任务目录里除最终产物外不落任何中间文件。
                            if cache_dir is None:
                                tmp.replace(dest)
                            else:
                                tmp.replace(cache_dir / local_name)
                                if not self._try_promote_from_cache(local_name, dest, cache_dir):
                                    raise RuntimeError(
                                        f"failed to place {local_name} into the task directory")

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
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            if _stop_requested():
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
                                if _stop_requested():
                                    break
                                step = min(0.5, delay - elapsed)
                                await asyncio.sleep(step)
                                elapsed += step

                    await _report_progress(progress_callback, granule, "failed", last_err, None)

            await asyncio.gather(*(one(g) for g in granules))

