#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cesiumlab_terrain.py
====================
Vendored from:
  E:/EarthVisLabApps/cesiumlab/4.0.17/python_terrain/cesiumlab_terrain.py

Purpose:
  DEM (any GDAL-readable raster) -> Cesium quantized-mesh-1.0 terrain tiles
  compatible with cesiumlab 4.0.17 output layout:
    out/
      layer.json
      meta.json
      {z}/{x}/{y}.terrain
"""

from __future__ import annotations

import argparse
import functools
import gzip
import io
import itertools
import json
import logging
import math
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np

from src.services.geo_validation import MAX_ZOOM

try:
    from osgeo import gdal, osr
except ImportError as e:
    # ImportError (not sys.exit/SystemExit) so `except Exception` callers — e.g.
    # dem_task_tiler's lazy-import wrapper and the tiling job thread — can catch
    # it and mark the job failed instead of dying silently with status 'running'.
    raise ImportError(
        "Missing GDAL Python bindings: pip install gdal (or conda install -c conda-forge gdal)"
    ) from e

# Avoid gdal.UseExceptions() here. It attempts to import osgeo.gdal_array, which may be absent
# depending on how GDAL Python bindings were built. This tiler doesn't require gdal_array.

logger = logging.getLogger(__name__)

WGS84_A = 6378137.0
WGS84_B = 6356752.3142451793
WGS84_E2 = 1.0 - (WGS84_B * WGS84_B) / (WGS84_A * WGS84_A)

DEG_TO_M = 111320.0          # 赤道处 1° **经度** 的米数，仅用于 max_error 的量级换算
DEFAULT_MAX_ERROR_K = 0.15   # 允许高程误差 = K * 顶点间距（见 _max_error_for_level）


def lonlat_to_ecef(lon_deg: np.ndarray, lat_deg: np.ndarray, h: np.ndarray) -> np.ndarray:
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h) * cos_lat * np.cos(lon)
    y = (n + h) * cos_lat * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h) * sin_lat
    return np.stack([x, y, z], axis=-1)


def _vertex_normals_ecef(lons: np.ndarray, lats: np.ndarray,
                         heights: np.ndarray) -> np.ndarray:
    """逐顶点法线（面积加权），在 ECEF 空间计算。输入 (n,n)，输出 (n,n,3) 单位向量。

    **必须用 ECEF 而不是经纬度空间**：经纬度下 1° 经距按 cos(纬度) 收缩，直接在
    (lon·111320, lat·111320, h) 上叉积等于把地形东西向拉伸 1/cos(lat) 倍，法线
    随纬度系统性偏斜。实测把同一块真实地形（天山 N42E086 的 z11 瓦片，65×65，
    高差 1758 m）搬到各纬度，把经纬度空间的结果按 ENU→ECEF 转回来再比，
    夹角中位数：lat0 0.05° / lat20 0.45° / lat42 **2.37°** / lat60 6.80° /
    lat80 28.40°。赤道≈0、随纬度单调放大，正是「东西向拉伸」这个解释本身的验证。

    lons/lats 可以是 (n,n) 网格，也可以是 (1,n)/(n,1) 的广播轴 —— 后者让
    lonlat_to_ecef 里的 radians/sin/cos/sqrt 只算 n 次而不是 n²，结果与
    meshgrid 版**逐位相同**（5 组 bbox × 3 种 n × 2 种高程场实测 0 差异），
    heights 的形状 (n,n) 会把结果撑回 (n,n,3)。_tile_normals 走的是广播那条。

    三角形切法与 _mesh_constants 一致（每格两片：(a0,a1,a2) 与 (a1,a3,a2)），
    权重用未归一化的叉积 —— 其模长 = 2×三角形面积，累加即天然面积加权。
    最后统一朝外（与地心方向点积为正），因此**不依赖三角形绕序**：RTIN 绕向
    在本轮改过一次（dad22bd77），法线不该再被同一件事绊一遍。
    """
    xyz = lonlat_to_ecef(lons, lats, heights)
    acc = np.zeros_like(xyz)
    v0 = xyz[:-1, :-1]      # a0 = (j,   i)
    v1 = xyz[:-1, 1:]       # a1 = (j,   i+1)
    v2 = xyz[1:, :-1]       # a2 = (j+1, i)
    v3 = xyz[1:, 1:]        # a3 = (j+1, i+1)
    n1 = np.cross(v1 - v0, v2 - v0)      # 三角形 (a0,a1,a2)
    n2 = np.cross(v3 - v1, v2 - v1)      # 三角形 (a1,a3,a2)
    # 每片面法线散给它的三个顶点。切片是基本索引（视图），+= 就地累加，
    # 不需要 np.add.at —— 六条语句各自命中的元素集合内部无重复。
    for sl, nr in ((np.s_[:-1, :-1], n1), (np.s_[:-1, 1:], n1), (np.s_[1:, :-1], n1),
                   (np.s_[:-1, 1:], n2), (np.s_[1:, 1:], n2), (np.s_[1:, :-1], n2)):
        acc[sl] += nr
    ln = np.linalg.norm(acc, axis=-1, keepdims=True)
    ln[ln == 0] = 1.0
    out = acc / ln
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    out[(out * up).sum(-1) < 0] *= -1
    return out


def _oct_encode(normals: np.ndarray) -> np.ndarray:
    """单位向量 (N,3) -> oct16 编码 (N,2) uint8。

    quantized-mesh-1.0 的 Oct-Encoded Per-Vertex Normals 扩展（extensionId=1）。
    算法与 Cesium 的 `AttributeCompression.octEncode` 一致：先按 L1 范数投影到
    八面体，z<0 的那一半折叠到外圈（|x|+|y|>1 就是解码端区分上下半球的唯一
    依据），再把 [-1,1] 映射到 [0,255]。

    **舍入必须是 half-up，不能用 np.round。** Cesium 的 toSNorm 是
    `Math.round((clamp(e,-1,1)*.5+.5)*255)`，而 JS 的 Math.round 平局一律进位；
    numpy 的 np.round 是 half-to-even。平局不是理论边界：t=(p*0.5+0.5)*255
    精确落在 k+0.5 上的 double 实测有 2164 个（在 p=(2(k+0.5)/255)-1 附近用
    nextafter 逐 double 扫出来的），其中 744 个 k 是偶数 —— 那些点上两种舍入
    差 1 个 LSB。
    ⚠️ **别把它说成"修了一个 bug"**：实测真实地形法线（天山 z9-12 共 36 张
    瓦片、304200 个分量）平局出现 **0 次**，且 1 个 LSB 的视觉后果本来也可以
    忽略。这里要的是"与 Cesium 的编码器逐位一致"这句话能成立而不是大概成立 ——
    代价是一行 floor，那就没有理由用一个已知会分歧的写法。
    x 恒在 [0,255]：floor 与 x-floor(x) 都是精确运算，所以下面这个写法与
    ES 规范里 Math.round 对 x>=0 的定义逐位相同。

    l1==0 的兜底：_vertex_normals_ecef 的输出是单位向量（模长为 0 的地方被置成
    1 再除），L1 范数因此恒 >=1，生产路径到不了这里；留着是因为零向量会让 p 变
    NaN，而 NaN 转 uint8 在 numpy 里只刷一条 RuntimeWarning（默认不中断）、
    落出来的字节值是平台相关的 —— 又是一条"不报错但字节是垃圾"的路。
    """
    v = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    l1 = np.abs(v).sum(axis=1, keepdims=True)
    l1 = np.where(l1 == 0.0, 1.0, l1)
    p = v[:, :2] / l1
    # ⚠️ 这里的 z 是 **ECEF 的 Z 轴**（指北极），不是"法线朝下"：法线基本沿地心
    # 方向，符号约等于 sin(纬度)。实测同一块地形搬到各纬度，z<0 的比例
    # lat-42 100% / lat0 45% / lat+5 0.9% / lat+42 **0%** —— 折叠分支是**南半球
    # 专属**路径，本项目手头的 DEM 全在 N28~N52，删掉它在现有数据上零症状。
    neg = v[:, 2] < 0.0
    if neg.any():
        px = p[neg, 0].copy()
        py = p[neg, 1].copy()
        # signNotZero: e<0 ? -1 : 1 —— 0 和 -0.0 都算正号（JS 里 -0<0 为 false，
        # numpy 里 -0.0>=0.0 为 True，两边一致）
        p[neg, 0] = (1.0 - np.abs(py)) * np.where(px >= 0.0, 1.0, -1.0)
        p[neg, 1] = (1.0 - np.abs(px)) * np.where(py >= 0.0, 1.0, -1.0)
    t = (np.clip(p, -1.0, 1.0) * 0.5 + 0.5) * 255.0
    floor = np.floor(t)
    return (floor + (t - floor >= 0.5)).astype(np.uint8)


@dataclass
class GeographicTilingScheme:
    tile_size: int = 17

    def tile_count(self, level: int) -> Tuple[int, int]:
        return 2 << level, 1 << level

    def tile_extent_deg(self, level: int, x: int, y: int) -> Tuple[float, float, float, float]:
        nx, ny = self.tile_count(level)
        west = -180.0 + 360.0 * x / nx
        east = -180.0 + 360.0 * (x + 1) / nx
        south = -90.0 + 180.0 * y / ny
        north = -90.0 + 180.0 * (y + 1) / ny
        return west, south, east, north

    def estimate_max_level(self, pixel_size_deg: float) -> int:
        if pixel_size_deg <= 0:
            return 14
        # 瓦片顶点网格是 tile_size x tile_size（见 _worker_tile 的 linspace：
        # 端点含边界），即 tile_size-1 个采样间隔均分瓦片的 180°/2^z 纬度跨度；
        # 间隔追上源像素尺寸即可。此前硬编码 180/64（=65 顶点网格），无视
        # self.tile_size，tile_size=17 时自动层级少算约 2 级（M22）。
        intervals = max(1, self.tile_size - 1)
        deg_per_tile_pixel = 180.0 / intervals
        ratio = deg_per_tile_pixel / pixel_size_deg
        return max(0, int(math.ceil(math.log2(ratio))))


def intersecting_tile_range(nx: int, ny: int, west: float, south: float,
                            east: float, north: float) -> Tuple[int, int, int, int]:
    """与 [west,east]x[south,north] 真正相交的瓦片索引闭区间 (ix0, ix1, iy0, iy1)。

    上界**不能用 floor**:瓦片是半开区间 [w,e) / [s,n),DEM 四至恰好落在瓦片边界上
    时 floor 会多算一格,而那一格与 DEM 零重叠。这不是罕见情形 —— granule 是
    1°×1°、四至是整度,而 (45+90)/180*2^z = 0.75*2^z 对 z≥2 恒为整数,所以凡是
    北界落在 45 的倍数上的 granule,**每一级都会多出一整行**;东界在
    {0,±45,±90,±135} 上同理多一列。

    多出来的那一行不是空的:它照样出图(DemSampler 把读窗口外扩 1px 后 ix/iy 被
    np.clip 钳死 → 一张「最北一行像素向北抹平」的台地),而且会被写进 layer.json 的
    available。Cesium 取「首个声明可用的层」,不会回落到 base/parentUrl,于是用户
    看到真实地形旁边贴着一块几十度宽的假高原,HTTP 全 200、作业 completed、零报错。
    M12 收窄 available 就是为了堵这条链,这里补上残留的 off-by-one。
    见 docs/reviews/2026-08-08-full-project-review.md 的 T1。

    `ceil(...) - 1` 在非边界值上与 `floor` 完全等价;外层 max(ix0, ...) 兜住
    「DEM 窄于一格且整体贴在边界上」时上界掉到下界之下。
    """
    ix0 = max(0, int(math.floor((west + 180.0) / 360.0 * nx)))
    ix1 = min(nx - 1, max(ix0, int(math.ceil((east + 180.0) / 360.0 * nx)) - 1))
    iy0 = max(0, int(math.floor((south + 90.0) / 180.0 * ny)))
    iy1 = min(ny - 1, max(iy0, int(math.ceil((north + 90.0) / 180.0 * ny)) - 1))
    return ix0, ix1, iy0, iy1


def _overview_factors(width: int, height: int, min_side: int = 32) -> list[int]:
    """2 的幂倍率列表，一直建到最长边降不到 min_side 以下为止。

    **为什么必须是 2 的幂** —— 理由是「让请求精确命中某一层」,不是「防漂移」:
    DemSampler._align 保证 win 恒等于 buf*scale 且 scale = 1<<k,所以采样器请求的
    降采样比值恒为 2 的幂;overview 层级也取 2 的幂,该比值就精确落在某一层上。
    换成 ASTER 那套(2/3/4/7.98/8.98/...),一个 ratio 恰为 8.000 的请求会落到 9x 那层,
    读到的是比要求更粗的数据。

    ⚠️ 不要把这里的理由写成「非幂次会让选层随窗口漂移」—— 那个因果只在**多源 VRT**
    上成立,而本函数只作用于已经物化的**单幅**栅格。单幅上带着 ASTER 的非幂次
    overview 实测也是 0.0 m(scale=8/16/32 三档 × 四个窗口),不会漂。反过来说,
    幂次集合也不是护身符:实测 [2,4,8,16,32] 在比值 6.36 时照样从 8x 掉到 4x ——
    真正保证不漂的是「比值恒为 2 的幂」这个**请求侧**的性质。

    min_side 取 32 而不是 contour 那边的 256(见 contour_engine._build_raster_overviews):
    等高线瓦片是 256px,金字塔到一瓦片大小就够;地形瓦片的顶点网格只有 65,低 zoom
    时窗口能覆盖整个 DEM 却只采 65²,需要更深的金字塔。
    """
    factors: list[int] = []
    f = 2
    while max(width, height) // f >= min_side:
        factors.append(f)
        f *= 2
    return factors


def _raise_on_gdal_error(what: str, path: str) -> None:
    """把 CPL 里登记的错误变成异常。

    **`ds is None` / `BuildOverviews() != 0` 挡不住 I/O 写失败。** 实测:磁盘满、
    配额、RLIMIT_FSIZE、超 4 GiB 时,gdal.Translate 照样返回**非 None** 的 dataset、
    BuildOverviews 照样返回 **0**,交出去的却是一份数据被截断的栅格 —— 打开正常、
    尺寸正常、overview 层数正常,只是降采样读回来全是 0。端到端实测的后果:693 张
    瓦片全部"渲染成功"、作业 completed、瓦片全 200、前端零报错,而 4000 m 的天山
    在 Cesium 里是一块海平面平板。

    模块顶部有意不开 gdal.UseExceptions()(见文件头注释),所以这些错误只落到 CPL
    的错误栈里。配合 ErrorReset() 前置,这里就能把它们捞出来。
    """
    if gdal.GetLastErrorType() >= gdal.CE_Failure:
        raise RuntimeError(
            f"{what} failed for {path}: {gdal.GetLastErrorMsg()!r} "
            f"(GDAL error {gdal.GetLastErrorNo()})")


def _assert_no_input_dropped(inputs: list, vrt_path: str) -> None:
    """BuildVRT 丢掉一个源时**不会失败**,必须自己查。

    它只打一行 `Warning 1: Can't open <file>. Skipping it`,返回一个完全有效的
    dataset。致命之处在于下游校验是**自洽**的:_verify_materialised 拿产物与
    **这个已经缺了源的 VRT** 比对,尺寸/dtype/仿射/逐像素处处一致,于是全程无
    异常。结果是一批 granule 里坏了一幅,地形任务「成功」但少切一整块地 ——
    瓦片全 200、layer.json 正常、前端零报错,正是这个项目反复吃过的静默失败。

    实测(GDAL 3.11.4,两幅相邻 600x600 拼 1200x600):
      - 垃圾内容 / 0 字节 / 文件不存在 => BuildVRT 跳过,VRT 只剩 600x600,
        build_input_raster 静默返回,**少切 50% 的地**。这三种正是本函数要挡的。
      - 头部完好的**截断**文件 => BuildVRT 不跳过(VRT 仍 1200x600、
        gdal.Open 照样成功、尺寸照样报 600x600),坏在读取阶段,由 Translate
        失败 + _verify_materialised 抓住,不归本函数管。

    三道闸门,缺一不可:
      1. 逐个 gdal.Open 探测 —— 能点名是**哪个**文件坏了,错误信息最有用。
      2. 拿 VRT 的 GetFileList() 与 inputs 对账 —— 这道才是彻底的:它直接问
         「你到底用了哪些文件」,不依赖我们去枚举 GDAL 的跳过原因。
         **不要退回成只查包围盒**:被跳过的源如果位于并集**内部**(A|B|C 里的 B),
         包围盒纹丝不动。实测两种触发且第 1、3 道都看不见的情况 ——
         `does not support heterogeneous band numbers`(B 是 3 波段)和
         `heterogeneous band data type`(B 是 Float32):产物宽度看着完全正确,
         中间那一整块却全是 0,任务照报 completed。
      3. 四至并集比对 —— 兜住「源被采纳但范围仍然不对」这类剩余情形,成本极低。
    """
    unreadable = []
    ext = None          # [minx, miny, maxx, maxy] —— 所有可读源的并集
    tol = 0.0           # 容差取最大像素边长,只抓整幅缺失,不抓浮点噪声
    sig = {}            # realpath -> (波段数, 数据类型名),仅用于诊断信息
    for p in inputs:
        ds = None
        try:
            ds = gdal.Open(p, gdal.GA_ReadOnly)
            if ds is None:
                unreadable.append(p)
                continue
            sig[os.path.realpath(p)] = (
                ds.RasterCount,
                gdal.GetDataTypeName(ds.GetRasterBand(1).DataType))
            gt = ds.GetGeoTransform()
            w, h = ds.RasterXSize, ds.RasterYSize
            xs = (gt[0], gt[0] + gt[1] * w + gt[2] * h)
            ys = (gt[3], gt[3] + gt[4] * w + gt[5] * h)
            e = (min(xs), min(ys), max(xs), max(ys))
            ext = list(e) if ext is None else [
                min(ext[0], e[0]), min(ext[1], e[1]),
                max(ext[2], e[2]), max(ext[3], e[3])]
            tol = max(tol, abs(gt[1]), abs(gt[5]))
        finally:
            ds = None   # Windows 上不放手就删不掉,见 _verify_materialised 的注释

    if unreadable:
        raise RuntimeError(
            f"{len(unreadable)}/{len(inputs)} 个输入 DEM 打不开,BuildVRT 会静默跳过"
            f"它们、少切这片地而任务仍报成功: {unreadable}")

    if ext is None:
        return

    vds = None
    try:
        vds = gdal.Open(vrt_path, gdal.GA_ReadOnly)
        if vds is None:
            raise RuntimeError(f"cannot reopen VRT for coverage check: {vrt_path}")
        used = {os.path.realpath(f) for f in (vds.GetFileList() or [])}
        gt = vds.GetGeoTransform()
        w, h = vds.RasterXSize, vds.RasterYSize
        xs = (gt[0], gt[0] + gt[1] * w + gt[2] * h)
        ys = (gt[3], gt[3] + gt[4] * w + gt[5] * h)
        vext = (min(xs), min(ys), max(xs), max(ys))
    finally:
        vds = None

    dropped = [p for p in inputs if os.path.realpath(p) not in used]
    if dropped:
        detail = ", ".join(
            f"{os.path.basename(p)}(波段数,类型)={sig.get(os.path.realpath(p))}"
            for p in dropped)
        raise RuntimeError(
            f"BuildVRT 排除了 {len(dropped)}/{len(inputs)} 个源,地形会缺一块而任务"
            f"仍报成功: {dropped} —— {detail};首个源是 "
            f"{sig.get(os.path.realpath(inputs[0]))}。BuildVRT 要求所有源的波段数、"
            f"数据类型、投影一致,不一致的会被静默跳过。")

    t = max(tol, 1e-9) * 1.5
    if (vext[0] > ext[0] + t or vext[1] > ext[1] + t
            or vext[2] < ext[2] - t or vext[3] < ext[3] - t):
        raise RuntimeError(
            f"VRT 覆盖范围 {vext} 小于 {len(inputs)} 个输入的并集 {ext} —— "
            f"BuildVRT 排除了至少一个源,切出来的地形会缺一块 ({vrt_path})")


def _verify_materialised(out_path: str, vrt_path: str) -> None:
    """物化产物必须与源**逐项对得上**,不能只看「文件建出来了」。

    这一层是给「GDAL 不报错但产物是坏的」兜底的,两类实测过的形态:
      - 写失败被静默吞掉 => 尺寸/层数全对,但降采样读回来全 0(整幅塌成海平面)
      - Translate 参数被改坏(如误加 outputType=GDT_Byte 想"省空间")=> 整幅 DEM
        塌成常数 255,而 11 个单元测试 + 153 个切片相关测试**全部通过**

    所以校验的是与源的一致性(尺寸/数据类型/仿射变换)加上一次真实的降采样读 ——
    低层级瓦片走的正是降采样路径,全 0 在这里当场暴露。

    ⚠️ **整个函数体挂在 try/finally 上,退出前把 dataset 与 band 全部置空。**
    本函数抛出时,异常的 traceback 会钉住这个栈帧,帧里的 src/dst/sb/db 于是一直
    活着 —— Windows 上文件被占着就删不掉,调用方 `build_input_raster` 的清理
    静默失败(那里 `except OSError: pass`),留下一份与源同量级的坏产物。
    v0.2.8 的发版 CI 实测:Linux 全绿、Windows 报「坏产物没被删除」—— Linux
    允许 unlink 仍被打开的文件,所以这个失败面在 Linux 上完全看不见。
    band 也要置空:它自己持有 dataset 引用,只放 dataset 不够。
    """
    src = dst = sb = db = None
    try:
        src = gdal.Open(vrt_path, gdal.GA_ReadOnly)
        dst = gdal.Open(out_path, gdal.GA_ReadOnly)
        if src is None or dst is None:
            raise RuntimeError(f"cannot reopen for verification: {out_path}")

        if (dst.RasterXSize, dst.RasterYSize) != (src.RasterXSize, src.RasterYSize):
            raise RuntimeError(
                f"materialised raster size {dst.RasterXSize}x{dst.RasterYSize} != "
                f"source {src.RasterXSize}x{src.RasterYSize} ({out_path})")

        sb, db = src.GetRasterBand(1), dst.GetRasterBand(1)
        if db.DataType != sb.DataType:
            raise RuntimeError(
                f"materialised raster dtype {gdal.GetDataTypeName(db.DataType)} != "
                f"source {gdal.GetDataTypeName(sb.DataType)} ({out_path}) —— "
                "高程会被静默压平/截断")

        sgt, dgt = src.GetGeoTransform(), dst.GetGeoTransform()
        if max(abs(a - b) for a, b in zip(sgt, dgt)) > 1e-12:
            raise RuntimeError(
                f"materialised raster geotransform {dgt} != source {sgt} "
                f"({out_path})")

        # --- 与源逐像素比对,必须走**全分辨率** ---
        # 不能拿降采样读去比:多源 VRT 的降采样读正是本次修复的那条坏路径(选层随
        # 窗口漂移),拿它当基准等于要求产物复现同一个 bug。全分辨率读 buf==win,
        # 不进 overview 路径,两边都是准的。
        # 右下角这块是冲着**截断**去的:写盘被截断时丢的总是后写的部分,实测 4 GiB
        # 截断的文件左上角完好(-460.6)、右下角全 0。
        w = min(64, dst.RasterXSize)
        h = min(64, dst.RasterYSize)
        for xoff, yoff, corner in ((0, 0, "左上"),
                                   (dst.RasterXSize - w, dst.RasterYSize - h, "右下")):
            gdal.ErrorReset()
            got = db.ReadAsArray(xoff, yoff, w, h)
            _raise_on_gdal_error("verification read", out_path)
            if got is None:
                raise RuntimeError(
                    f"materialised raster unreadable at {corner} corner "
                    f"({xoff},{yoff}) ({out_path}) —— 多半是写盘被截断了")
            ref = sb.ReadAsArray(xoff, yoff, w, h)
            if ref is None:
                raise RuntimeError(
                    f"cannot read source for verification: {vrt_path}")
            if not np.array_equal(np.asarray(got), np.asarray(ref)):
                raise RuntimeError(
                    f"materialised raster disagrees with source at {corner} corner "
                    f"({out_path}) —— 产物不是源的忠实副本")

        # --- overview 健全性 ---
        # BuildOverviews 在写失败时照样返回 0,建出来的层是**空的**:全分辨率读全对、
        # 降采样读全 0。低层级瓦片走的正是降采样路径,于是整片地形塌成海平面平板而
        # 作业报 completed。这里拿同一个窗口的两种读法对照 —— 全分辨率有起伏而降采样
        # 塌成常数,就是 overview 坏了。(源数据本身是常数的区域跳过,不误报。)
        if db.GetOverviewCount() > 0:
            pw = min(512, dst.RasterXSize)
            ph = min(512, dst.RasterYSize)
            gdal.ErrorReset()
            full = db.ReadAsArray(0, 0, pw, ph)
            small = db.ReadAsArray(0, 0, pw, ph,
                                   buf_xsize=max(1, pw // 8),
                                   buf_ysize=max(1, ph // 8))
            _raise_on_gdal_error("overview probe", out_path)
            if full is None or small is None:
                raise RuntimeError(f"materialised raster overview probe failed "
                                   f"({out_path})")
            full_a, small_a = np.asarray(full), np.asarray(small)
            if full_a.min() != full_a.max() and small_a.min() == small_a.max():
                raise RuntimeError(
                    f"materialised raster has empty overviews ({out_path}): "
                    f"full-res range [{full_a.min()}, {full_a.max()}] but the "
                    f"downsampled read is constant {small_a.min()} —— "
                    "低层级瓦片会整片塌成平板")
    finally:
        sb = db = src = dst = None


def _gdal_stage_callback(stage_cb, phase: str):
    """把 GDAL 的原生进度回调适配成 stage_cb(phase, fraction)。

    ⚠️ **回调里绝不能抛异常**。实测(GDAL 3.8.4):回调抛出时 GDAL 把它当成
    「用户请求中止」—— 打印 `ERROR 9: User terminated CreateCopy()`、
    `gdal.Translate` 返回 **None**、产物文件被删掉。也就是说一次 emit 故障
    (客户端断开等)就能把整个切片搞成失败。所以这里整个包死。

    返回 0 才是「中止」的正常表达方式;这里恒返回 1(继续)。
    """
    if stage_cb is None:
        return None

    def _cb(complete, message, cb_data):
        try:
            stage_cb(phase, float(complete))
        except Exception:
            pass
        return 1

    return _cb


def build_input_raster(inputs: list[str], work_dir: str | None = None,
                       stage_cb=None) -> str:
    """把切片输入归一成**单幅**栅格路径。单幅直通,多幅物化。

    ## 为什么多幅必须物化,而不是交一个 VRT 出去

    ASTER 源 GeoTIFF 自带 8 层内嵌 overview,倍率是 2/3/4/**7.98**/**8.98**/
    15.9/63/80 —— 不是 2 的幂。降采样读取时 GDAL 会自动切到 overview,而多源 VRT
    要先按 source 切分请求再分别转发,每段的 win/buf 比值不再整齐,挑中的 overview
    层随读窗口漂移。相邻瓦片的读窗口本来就不同(南瓦片 win=664/buf=83、北瓦片
    win=656/buf=82),于是同一个经纬点被采成不同高程。

    实测(6 幅天山 ASTER):z10 有 16 对南北相邻瓦片公共边对不上,**最大 50.9 m**;
    同一条纬线在 z11/z12 却精确一致(那两层 scale=4/2,够不到 8x 与 9x 这对挨得近的
    档位)。单幅路径免疫 —— scale=8/16/32 三档、带与不带 overview 各四个窗口,
    全部 0.0 m。物化后同一批瓦片 z9/z10/z11 共 1063 对公共边全部归零。

    GDAL 侧无解:`OVERVIEW_LEVEL=NONE` 只关掉 VRT **自己**的虚拟 overview,
    源仍由 VRTSimpleSource 转发后自行选层(实测差值纹丝不动,仍是 19 m)。

    ## 为什么物化之后还要补 overview

    Translate 默认不建 overview,低层级瓦片会退化成全分辨率读取:实测 z6 从
    0.2 ms/张掉到 13.0 ms/张。补上 2 的幂 overview 后反而快过现状(裸 VRT 2.3 ms)。

    ## 代价

    切片期间多占一份磁盘。实测 6 幅 ASTER(Int16,源 118 MB):物化 + 8 层 overview
    共 91.8 MB(源的 78%),耗时 **13.6 秒**。
    ⚠️ 78% 这个比例是 **Int16 + DEFLATE** 的:默认数据集 Copernicus GLO-30 是
    **Float32**,实测物化后约为源的 **1.9 倍** —— 按最坏情况给磁盘留量。
    产物由 build_terrain 的 finally 删除;本函数自身失败时在这里就地删除。
    """
    if len(inputs) == 1:
        return inputs[0]

    import tempfile
    import time

    # 这一步是纯静默的等待：6 幅 ASTER 实测 13.6 秒,大任务到分钟级。不出声的话
    # 切片入口看起来就是卡住了(contour 的 warp 同理,那边早就有这条日志)。
    logger.info(f"Terrain: 合并 {len(inputs)} 个 DEM 为单文件"
                f"(多源 VRT 会开高程接缝),大区域可能耗时数十秒...")
    _t0 = time.time()

    # 文件名里带 pid,对齐 download_engine 的 `*.part.<pid>.<id>` 形态。这不是
    # 装饰:正常路径和失败路径都会删它,但 SIGKILL/关窗盖不住,残留只能靠下次启动
    # 清扫(task_cleanup.sweep_startup_residue)。而那边唯一可靠的归属判据就是 pid
    # —— mtime 判据在这里近乎无效:物化产物写完 mtime 就冻住,而切片可以再跑几
    # 小时,晚起的第二个实例会认为它「早于我启动」而放行删除。
    fd, out_path = tempfile.mkstemp(
        suffix=".tif", prefix=f"cesiumlab_terrain_{os.getpid()}_", dir=work_dir)
    os.close(fd)
    # mkstemp 建的空文件会让 Translate 报「已存在」,先让位
    try:
        os.remove(out_path)
    except OSError:
        pass

    vrt_path = out_path + ".vrt"
    try:
        try:
            # ⚠️ 显式钉死「非异常」模式,不要依赖没人开过 gdal.UseExceptions()。
            # 那个开关是**进程全局**的:四条流水线共用一个 Flask 进程,contour_engine
            # 无条件调它,于是用户跑过任意一个等高线任务之后,这里的语义就被换掉了。
            # 后果是下面每一处 _raise_on_gdal_error 退化成空转 —— 异常模式下 GDAL 把
            # CE_Failure 直接抛成 Python 异常、不回填 CPL 错误栈,而这个函数捞的正是
            # 那个栈。第一道闸门没了,只剩 _verify_materialised 兜底。
            # 用 ExceptionMgr 就地声明本段所需的模式,退出时自动还原,既不受别人影响、
            # 也不影响别人;GDAL 4.0 把默认翻成异常模式后这段依然成立(3.7+ 均支持)。
            with gdal.ExceptionMgr(useExceptions=False):
                opts = gdal.BuildVRTOptions(resampleAlg="bilinear", addAlpha=False)
                vrt = gdal.BuildVRT(vrt_path, inputs, options=opts)
                if vrt is None:
                    raise RuntimeError(f"gdal.BuildVRT failed for inputs={inputs}")
                vrt.FlushCache()
                vrt = None
                # BuildVRT 返回非 None 不代表所有源都进去了 —— 打不开的会被静默跳过,
                # 而后面所有校验都是拿产物跟这个已经缺了源的 VRT 比,永远自洽。
                _assert_no_input_dropped(inputs, vrt_path)

                # 物化。失败必须响 —— 悄悄交出坏产物就是一份平板地形,而任务标记成功、
                # 瓦片全 200、前端一条错误都没有。光看返回值不够,见 _raise_on_gdal_error。
                #
                # BIGTIFF=IF_SAFER 不能省:GTiff 的默认 IF_NEEDED 只在**不压缩**时按未压缩
                # 体积判断是否升级 BigTIFF,一旦带了 COMPRESS 就一律按经典 TIFF(4 GiB 上限)
                # 建文件。实测 ~92 幅 Copernicus granule(约 10°×10°)就到顶,而超出部分被
                # 静默丢弃、两道返回值闸门一个都不触发。contour_engine 的两处 Translate
                # 早就写了 IF_SAFER,这里对齐它。
                gdal.ErrorReset()
                ds = gdal.Translate(
                    out_path, vrt_path, format="GTiff",
                    creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "ZLEVEL=1",
                                     "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"],
                    callback=_gdal_stage_callback(stage_cb, "merge"))
                if ds is None:
                    raise RuntimeError(
                        f"gdal.Translate failed to materialise {len(inputs)} inputs "
                        f"into {out_path}")
                width, height = ds.RasterXSize, ds.RasterYSize
                ds.FlushCache()
                ds = None
                _raise_on_gdal_error("gdal.Translate", out_path)

                factors = _overview_factors(width, height)
                if factors:
                    ds = gdal.Open(out_path, gdal.GA_Update)
                    if ds is None:
                        raise RuntimeError(
                            f"cannot reopen materialised raster {out_path}")
                    gdal.ErrorReset()
                    rc = ds.BuildOverviews(
                        "AVERAGE", factors,
                        callback=_gdal_stage_callback(stage_cb, "overview"))
                    ds = None
                    if rc != 0:
                        raise RuntimeError(f"BuildOverviews failed on {out_path}")
                    _raise_on_gdal_error("BuildOverviews", out_path)

                _verify_materialised(out_path, vrt_path)
        finally:
            try:
                os.remove(vrt_path)
            except OSError:
                pass
    except BaseException:
        # 写盘之后的任何失败都会留下一份与源同量级的半成品。清理挂在 build_terrain
        # 的 finally 上,而那个 finally 够不着 —— build_input_raster 是在它的 try 之外
        # 调用的,抛出时 input_path 根本没被赋值。而最可能触发失败的恰恰是磁盘不足:
        # 用户看到任务失败就重试,每重试一次再多留一份,可用空间被自己的失败残骸吃光。
        # 删不掉必须出声。这里原本是全静默的 `except OSError: pass`,于是
        # Windows 上「dataset 还开着 → 删除被拒 → 留下 GB 级坏产物」这条链
        # 一路无声,只有测试断言把它抓了出来。留残骸本身还有下次启动的清扫兜底
        # (task_cleanup 按文件名里的 pid 判归属),但用户得先知道发生过。
        for path in (out_path, out_path + ".ovr", out_path + ".aux.xml"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(
                    f"Terrain: 物化失败后删不掉半成品 {path}: {e} —— "
                    f"它与源同量级,下次启动会被清扫回收")
        raise
    try:
        size_mb = os.path.getsize(out_path) / 1048576
    except OSError:
        size_mb = 0.0
    logger.info(f"Terrain: DEM 合并完成, 耗时 {time.time() - _t0:.1f}s, "
                f"{size_mb:.1f} MB (切片结束后自动删除)")
    return out_path


class DemSampler:
    def __init__(self, path: str, nodata: float | None = None):
        src = gdal.Open(path, gdal.GA_ReadOnly)
        if src is None:
            raise FileNotFoundError(path)

        srs = osr.SpatialReference()
        wkt = src.GetProjection()
        if wkt:
            srs.ImportFromWkt(wkt)
        else:
            srs.ImportFromEPSG(4326)
        target = osr.SpatialReference()
        target.ImportFromEPSG(4326)

        if not srs.IsSame(target):
            src = gdal.AutoCreateWarpedVRT(src, srs.ExportToWkt(), target.ExportToWkt(), gdal.GRA_Bilinear)

        self.ds = src
        gt = src.GetGeoTransform()
        self.gt = gt
        self.cols = src.RasterXSize
        self.rows = src.RasterYSize

        band = src.GetRasterBand(1)
        if nodata is None:
            nodata = band.GetNoDataValue()
        self.nodata = nodata
        self.band = band
        self.pixel_size_deg = abs(gt[1])
        x0, y0 = gdal.ApplyGeoTransform(gt, 0, 0)
        x1, y1 = gdal.ApplyGeoTransform(gt, self.cols, self.rows)
        self.bounds = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def sample(self, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
        gt = self.gt
        det = gt[1] * gt[5] - gt[2] * gt[4]
        px = (gt[5] * (lons - gt[0]) - gt[2] * (lats - gt[3])) / det
        py = (-gt[4] * (lons - gt[0]) + gt[1] * (lats - gt[3])) / det

        x0 = int(math.floor(np.nanmin(px))) - 1
        y0 = int(math.floor(np.nanmin(py))) - 1
        x1 = int(math.ceil(np.nanmax(px))) + 1
        y1 = int(math.ceil(np.nanmax(py))) + 1
        x0c = max(0, x0)
        y0c = max(0, y0)
        x1c = min(self.cols, x1)
        y1c = min(self.rows, y1)
        if x1c <= x0c or y1c <= y0c:
            return np.zeros_like(lons, dtype=np.float32)

        # Let GDAL downsample oversized windows instead of reading them at
        # native resolution: low-zoom tiles over a large DEM would otherwise
        # allocate the full source window per worker process (OOM). Sampling
        # density never needs more than the output grid plus a small margin.
        #
        # **降采样格网必须锚定在源像素网格上，不能跟着请求的 bbox 走。**
        # 此前 scale 是 win/buf、而 win 由传入点集的包围盒决定 —— 相邻瓦片包围盒
        # 不同 ⇒ 降采样格子落在源像素的不同相位上 ⇒ 同一个经纬点采出不同高程。
        # 实测（1 弧秒合成 DEM，65² 网格）：相邻瓦片公共边最大差 20.2 m；真实
        # 天山数据上 z12 平均 3.4 m / 最大 16.8 m，而 z14 精确为 0（那层不降采样）。
        # 这是几何裂缝与「法线接缝在中间层无法归零」的共同根因，与三角化无关。
        #
        # 现在：scale 只由「输出网格步长 ÷ 源像素」决定 —— 同层级瓦片的步长相同，
        # 与瓦片落在哪里无关；再取 2 的幂，避免浮点误差把相邻瓦片挤到不同档。
        # 读窗口起点对齐到 scale 的整数倍，且 win 恒等于 buf*scale，于是 buffer
        # 像素 j 永远代表源像素 [x0a+j*scale, x0a+(j+1)*scale)，全局固定。
        span_x = float(np.nanmax(px) - np.nanmin(px))
        span_y = float(np.nanmax(py) - np.nanmin(py))
        step_x = span_x / max(1, lons.shape[1] - 1)
        step_y = span_y / max(1, lons.shape[0] - 1)
        scale_x = 1 << max(0, int(math.floor(math.log2(step_x)))) if step_x > 1 else 1
        scale_y = 1 << max(0, int(math.floor(math.log2(step_y)))) if step_y > 1 else 1

        def _align(lo, hi, limit, scale):
            """把 [lo,hi) 扩到 scale 网格上；返回 (起点, 窗口宽, buffer 宽)。

            起点向下对齐、终点向上对齐，两端各留一个 scale 的余量，保证边缘采样
            点仍落在 buffer 覆盖的范围内（GDAL 的重采样是中心制的）。
            """
            lo_a = max(0, ((lo // scale) - 1) * scale)
            hi_a = min(limit, (-(-hi // scale) + 1) * scale)
            nbuf = (hi_a - lo_a) // scale
            if nbuf < 1:                       # 源太小，退化成不降采样
                return lo, max(1, hi - lo), max(1, hi - lo)
            return lo_a, nbuf * scale, nbuf

        x0c, win_w, buf_w = _align(x0c, x1c, self.cols, scale_x)
        y0c, win_h, buf_h = _align(y0c, y1c, self.rows, scale_y)
        arr = self.band.ReadAsArray(
            x0c, y0c, win_w, win_h,
            buf_xsize=buf_w, buf_ysize=buf_h,
            resample_alg=gdal.GRA_Bilinear,
        ).astype(np.float32)
        if self.nodata is not None:
            arr = np.where(arr == self.nodata, np.nan, arr)

        # 把源像素坐标映射成 buffer 里的连续下标。
        #
        # px 是 GDAL 的**像素边**坐标（`ApplyGeoTransform(gt,0,0)` 给的是左上角，
        # 0 号像素中心在 0.5）。buffer 像素 j 覆盖源 [x0c+j*sx, x0c+(j+1)*sx)，
        # 其中心在边坐标 x0c+(j+0.5)*sx。令 px = x0c+(t+0.5)*sx 解出
        #     t = (px - x0c)/sx - 0.5
        # 两个 -0.5 是**同一个**：边→中心的转换，与 sx 无关。
        #
        # ⚠️ 这里曾经写成 `- 0.5*(1 - 1/sx)`，即 t + 0.5/sx —— 恒偏 **+0.5 个源
        # 像素**（1 弧秒 DEM 上是 15.5 m 的水平错位，每张瓦片每个层级都吃）。
        # M19 复核提过、被 test_sampling_has_no_half_pixel_bias 驳回，但那个测试
        # 用合成线性场 `WriteArray(a*i + b*j)` 又拿 `expected = a*px_edge+b*py_edge`
        # 对账 —— 把「arr[j,i] 位于边坐标 i」这个错误前提写进了基准，自洽所以测得过。
        #
        # 2026-08-08 用 GDAL 自己当裁判重判（真实 Copernicus GLO-30，把源 warp 到
        # 一个像素中心恰好落在查询点上的网格，`gdal.Warp` + GRA_Bilinear）：
        #     中心制 (px-0.5) vs Warp  RMS 0.0000 m
        #     现行式  (edge)   vs Warp  RMS 8.0056 m / 最大 21.2 m
        # 逐瓦片端到端复核（loess z14，grid 后端）RMS 6.835 m -> 0.511 m。
        # sx=1 的层级（1 弧秒源上是 z13/z14，占生产瓦片 93.8%）修正后与 Warp 逐点相同。
        sx = win_w / buf_w
        sy = win_h / buf_h
        lpx = (px - x0c) / sx - 0.5
        lpy = (py - y0c) / sy - 0.5
        ix = np.clip(np.floor(lpx).astype(int), 0, arr.shape[1] - 2)
        iy = np.clip(np.floor(lpy).astype(int), 0, arr.shape[0] - 2)
        fx = np.clip(lpx - ix, 0, 1)
        fy = np.clip(lpy - iy, 0, 1)
        v00 = arr[iy, ix]
        v10 = arr[iy, ix + 1]
        v01 = arr[iy + 1, ix]
        v11 = arr[iy + 1, ix + 1]
        v0 = v00 * (1 - fx) + v10 * fx
        v1 = v01 * (1 - fx) + v11 * fx
        v = v0 * (1 - fy) + v1 * fy
        v = np.where(np.isnan(v), 0.0, v).astype(np.float32)
        return v


def _zigzag(v: np.ndarray) -> np.ndarray:
    return ((v << 1) ^ (v >> 31)).astype(np.uint32)


def _high_water_mark_encode(indices: np.ndarray) -> np.ndarray:
    out = np.empty_like(indices, dtype=np.uint32)
    highest = 0
    for i, code in enumerate(indices):
        out[i] = highest - code
        if code == highest:
            highest += 1
    return out


def _hwm_encode(indices: np.ndarray) -> np.ndarray:
    """high-water-mark 编码的向量化版本。

    **要求 indices 是规范形式**：顶点按首次出现顺序编号（rtin_extract 的输出
    天然满足）。此时 highest 在位置 i 的值等于「前 i 个元素中出现过的不同顶点
    数」，可以用 cumsum 一次算出，不必逐元素循环。

    规则网格分支仍用 _high_water_mark_encode：那边的索引是行优先编号、不是
    规范形式，且靠 _mesh_constants 的 lru_cache 一个进程只算一次，没有向量化
    的必要。两者不可互换。
    """
    idx = np.asarray(indices, dtype=np.int64)
    _, first = np.unique(idx, return_index=True)
    is_first = np.zeros(len(idx), dtype=bool)
    is_first[np.sort(first)] = True
    highest = np.cumsum(is_first) - is_first
    return (highest - idx).astype(np.uint32)


def _zz_delta(a: np.ndarray) -> np.ndarray:
    """zigzag-delta 编码:行优先展平 -> 相邻差分 -> zigzag。原先是
    encode_quantized_mesh 里的闭包,提到模块级是为了让 _mesh_constants 复用
    同一份实现(字节流必须与原逻辑逐字节等价)。"""
    flat = a.reshape(-1).astype(np.int32)
    d = np.empty_like(flat)
    d[0] = flat[0]
    d[1:] = flat[1:] - flat[:-1]
    return _zigzag(d).astype(np.uint16)


# encode_quantized_mesh 里有一大块只依赖网格尺寸 n(=tile_size)的常量:三角形索引、
# 高水位编码、四条边索引、u/v 的 zigzag-delta。此前每瓦片重算一遍(n=17 时三角形
# 索引是双重 for 拼 24576 元素 list + 逐元素 Python 循环高水位编码),纯属浪费。
# worker 是独立进程,进程内按 n 缓存一次即可;每瓦片只剩高度相关的 hzz 要现算。
# 缓存用的就是原来的计算代码,保证 quantized-mesh 字节流逐字节等价
# (test_fix_terrain_mesh_indices 按 spec 逐字段解析字节流)。
@functools.lru_cache(maxsize=None)
def _mesh_constants(n: int):
    u = np.linspace(0, 32767, n, dtype=np.uint16)
    v = np.linspace(0, 32767, n, dtype=np.uint16)
    uu, vv = np.meshgrid(u, v)
    uzz = _zz_delta(uu)
    vzz = _zz_delta(vv)

    # indices: 2 triangles per cell
    idx = []
    for j in range(n - 1):
        for i in range(n - 1):
            a0 = j * n + i
            a1 = j * n + (i + 1)
            a2 = (j + 1) * n + i
            a3 = (j + 1) * n + (i + 1)
            idx.extend([a0, a1, a2])
            idx.extend([a1, a3, a2])
    indices = np.array(idx, dtype=np.uint32)
    encoded_indices = _high_water_mark_encode(indices)

    # edge indices
    ar = np.arange(n, dtype=np.uint32)
    west_idx = ar * n
    south_idx = ar
    east_idx = ar * n + (n - 1)
    north_idx = (n - 1) * n + ar
    return uzz, vzz, encoded_indices, (west_idx, south_idx, east_idx, north_idx)


def encode_quantized_mesh(west: float, south: float, east: float, north: float,
                          heights_grid: np.ndarray, mesh=None, normals=None) -> bytes:
    """编码一张 quantized-mesh 瓦片。

    mesh=None 时用完整规则网格（索引表走 _mesh_constants 的进程级缓存）。
    传入 mesh=(vertex_grid_indices, triangles) 时按该三角网编码 —— 顶点是
    格点的子集，triangles 是局部索引，rtin_extract 的输出直接可用。

    normals 是**满网格**的逐顶点法线 (n,n,3)（或已展平的 (n*n,3)）。传 mesh 时
    自动按 mesh[0] 取子集：数量跟随简化后的顶点，值仍来自满网格计算 ——
    粗几何 + 精细法线，类似 normal mapping。设计稿:209 定的就是这个，
    K 质量验证阶段的着色图给出了实测支撑：几何面晕渲从 K=0.15 起就有明显三角
    刻面，而逐顶点法线着色平滑无刻面。
    """
    n = heights_grid.shape[0]
    assert heights_grid.shape == (n, n)
    h_min = float(np.min(heights_grid))
    h_max = float(np.max(heights_grid))
    if h_max == h_min:
        h_max = h_min + 1.0

    lon_c = 0.5 * (west + east)
    lat_c = 0.5 * (south + north)
    h_c = 0.5 * (h_min + h_max)
    cx, cy, cz = lonlat_to_ecef(np.array([lon_c]), np.array([lat_c]), np.array([h_c]))[0]

    # 外接球半径：直接对【这张瓦片自己的 n×n 顶点网格】取最远点。
    #
    # 为什么不再用手挑的候选表。它先后被证伪两次：
    #   1. 最早只取 4 个 h_min 角点 + 中心点，漏了 h_max 那 4 个角点（同样的
    #      经纬跨度在更高高度上对应更大的物理距离）—— 实测缺 0.35 m（1° 瓦片）
    #      到 55 m（10° 瓦片）。
    #   2. 补齐 8 角点后 z0 仍然缺 15.1 km：z0 跨 lat -90..90，四个经纬角点
    #      全部退化到南北两极这两个物理点上，最远点跑到了西/东边界的中点
    #      （赤道上）。于是又追加了 4 个 h_max 边中点。
    # 第二次修补在当前切片方案下缺口确实归零，但**裕度也精确是零**：沿一条
    # 经线（经度固定、纬度变化）的最远点只有在经度跨度 ≳ 179.233° 时才会离开
    # 端点、跑到边的内部去，位置约 lat* ≈ -148.4·lat_c。z0 的经度跨度精确是
    # 180°，正落在这个退化区间里；它没出事的唯一原因是 z0 的 lat_c 恰好为 0
    # 于是 lat* = 0，那个内部极大点正好就是西边中点、被候选表撞上了。把 z0 的
    # 南边界挪 0.1° 就缺 255 m，挪 0.4° 缺 3528 m。**任何人改动切片方案的纬度
    # 分割都会把这个洞重新打开**，而这个位置已经栽过两次了。
    #
    # 顶点网格没有这个问题：它就是这张瓦片真正编码出去的那些点，不依赖任何
    # 「极值在哪」的推断。所有三角形都落在顶点的凸包里、球是凸集，所以包住全部
    # 顶点就等于包住全部几何；自适应网格的顶点是格点的子集，同样被覆盖。
    # 代价是 tile_size=65 时 4225 个点走一次向量化的 lonlat_to_ecef。
    #
    # 包不住的后果：Cesium 视锥剔除可能把边角恰在视锥边缘的瓦片误剔 ——
    # HTTP 200、任务 completed、前端不报错，瓦片就是不显示。
    # 用广播而不是 meshgrid：lon 只有 n 个不同值、lat 也只有 n 个，喂
    # (1,n) 与 (n,1) 进去，lonlat_to_ecef 里的 radians/sin/cos/sqrt 就只算 n 次而
    # 不是 n² 次，结果因广播仍是 (n,n,3) 且与 meshgrid 版逐位相同
    # （实测 n∈{65,257} × 3 组 bbox 全部 0 差异）。这不是提前优化 —— 实测
    # （tile_size=65）：候选表 encode 0.091 ms/次，meshgrid 版 0.376，广播版 0.214；
    # 整条切片链路（天山 z6-11 共 224 张、auto、交错 best-of-4）
    # 1.47~1.51 s -> meshgrid 版 +11%、广播版 +5.8%（1.55~1.59 s）。
    # heights_grid 的行是纬度（南->北）、列是经度（西->东），与 _worker_tile 里
    # meshgrid(lons, lats) 的形状一致。
    grid_xyz = lonlat_to_ecef(
        np.linspace(west, east, n, dtype=np.float64)[None, :],
        np.linspace(south, north, n, dtype=np.float64)[:, None],
        heights_grid,
    )
    radius = float(np.linalg.norm(grid_xyz - np.array([cx, cy, cz]), axis=-1).max())
    # 读端拿到的顶点是【量化过】的，与上面这些精确格点差一点点：高程差最多一个
    # 量化步 (h_max-h_min)/32767，经纬各差最多一个 u/v 步长。距离函数是
    # 1-Lipschitz 的，位置误差的上界可以原样加进半径 —— 于是「包住」这件事不再
    # 依赖任何实测，而是可证的。这一项在 1° 瓦片上约 7 m（相对 1e-4）、z0 上约
    # 1.2 km（相对 1.4e-4），比要防的 15 km 缺口小四个数量级；而方向上宁大勿小：
    # 半径偏大只是少剔掉一点瓦片，偏小就是地形直接看不见。
    radius += (h_max - h_min) / 32767.0 + (WGS84_A + abs(h_max)) * math.radians(
        abs(east - west) + abs(north - south)
    ) / 32767.0

    a = WGS84_A
    b = WGS84_B
    if a > 0 and b > 0:
        sx_e, sy_e, sz_e = cx / a, cy / a, cz / b
        s_mag = math.sqrt(sx_e * sx_e + sy_e * sy_e + sz_e * sz_e) or 1.0
        nx_e, ny_e, nz_e = sx_e / s_mag, sy_e / s_mag, sz_e / s_mag
        scale = max(s_mag, 1.0) + (h_max + radius) / a
        hox = nx_e * scale * a
        hoy = ny_e * scale * a
        hoz = nz_e * scale * b
    else:
        hox = hoy = hoz = 0.0

    header = (
        struct.pack("<ddd", float(cx), float(cy), float(cz))
        + struct.pack("<ff", h_min, h_max)
        + struct.pack("<ddd", float(cx), float(cy), float(cz))
        + struct.pack("<d", float(radius))
        + struct.pack("<ddd", float(hox), float(hoy), float(hoz))
    )

    hh_full = ((heights_grid - h_min) / (h_max - h_min) * 32767.0).astype(np.uint16)

    if mesh is None:
        uzz, vzz, encoded_indices, edge_indices = _mesh_constants(n)
        hzz = _zz_delta(hh_full)
        vertex_count = n * n
    else:
        vert_idx, tris = mesh
        vertex_count = len(vert_idx)
        rows = (vert_idx // n).astype(np.int64)
        cols = (vert_idx % n).astype(np.int64)
        u_axis = np.linspace(0, 32767, n).round().astype(np.uint16)
        uu = u_axis[cols]
        vv = u_axis[rows]
        uzz = _zz_delta(uu)
        vzz = _zz_delta(vv)
        hzz = _zz_delta(hh_full.reshape(-1)[vert_idx])
        encoded_indices = _hwm_encode(tris.reshape(-1))
        # 四条边索引：保留顶点中落在各边上的。
        #
        # 【槽位】是硬约束：Cesium 按 west/south/east/north 的槽位决定裙边（skirt）
        # 往哪个方向位移（createVerticesFromQuantizedTerrainMesh.js：west 经度 -δ、
        # east +δ、south 纬度 -δ、north +δ），串了就错。
        #
        # 【顺序】不是。quantized-mesh-1.0 spec 对边内顺序只字未提，且 Cesium 1.143.0
        # 拿到这四个数组后会无条件复制并各自重排（west 按 v 升序、east 按 v 降序、
        # south 按 u 降序、north 按 u 升序 —— 见该文件返回的 westIndicesSouthToNorth /
        # eastIndicesNorthToSouth / southIndicesEastToWest / northIndicesWestToEast），
        # 生产端给什么顺序都会被覆盖。这里按格点坐标升序只是为了输出确定性
        # （同输入同字节，便于 golden 比对），不是在满足 spec 要求。
        edge_indices = (
            np.where(cols == 0)[0][np.argsort(rows[cols == 0])].astype(np.uint32),
            np.where(rows == 0)[0][np.argsort(cols[rows == 0])].astype(np.uint32),
            np.where(cols == n - 1)[0][np.argsort(rows[cols == n - 1])].astype(np.uint32),
            np.where(rows == n - 1)[0][np.argsort(cols[rows == n - 1])].astype(np.uint32),
        )

    # quantized-mesh-1.0: index width is chosen by VERTEX COUNT (>65536 -> 32-bit),
    # not by the max encoded value. High-water-mark diffs wrap around, so the u16
    # branch must truncate them (astype(np.uint16)) rather than range-check them.
    index_dtype = np.uint32 if vertex_count > 65536 else np.uint16

    def pack_indices(arr: np.ndarray) -> bytes:
        return arr.astype(index_dtype).tobytes()

    body = io.BytesIO()
    body.write(struct.pack("<I", vertex_count))
    body.write(uzz.tobytes())
    body.write(vzz.tobytes())
    body.write(hzz.tobytes())

    # spec: "To enforce proper byte alignment, padding is added before the IndexData
    # to ensure 2 byte alignment for IndexData16 and 4 byte alignment for IndexData32."
    #
    # 到这里的绝对偏移 = 88(header) + 4(vertexCount) + 6*vertexCount（u/v/h 三段
    # uint16）。88 和 4 都是 4 的倍数，所以偏移 ≡ 2*vertexCount (mod 4) ——
    # uint16 分支永远已经对齐（补 0 字节，规则网格的 golden 指纹不受影响），
    # 而 tile_size 恒为 2^k+1 => vertexCount=(2^k+1)^2 恒为奇数 =>
    # **uint32 分支必然要补 2 字节**。
    #
    # 漏掉这 2 字节的后果是整段 IndexData 错位：实测 tile_size=257 编码端写
    # triangleCount=131072，Cesium 的 parseQuantizedMesh 按 spec 补齐后读到 **2**，
    # 整块地形塌成两个三角形，且 HTTP 全 200、任务 completed、前端不报错 ——
    # 与已修掉的 triangleCount bug 同一种失效形态。
    # 生产 tile_size 硬编码 65（uint16）故此前休眠；触发入口是 CLI --tile-size。
    # len(header) 而不是字面量 88：对齐是相对【整个瓦片】的起点算的，header 若变长
    # （Task 6/7 要加 extension 段是加在末尾，但难保没人动前面）这里要跟着变。
    body.write(b"\x00" * ((-(len(header) + body.tell())) % np.dtype(index_dtype).itemsize))

    # IndexData.triangleCount 是【三角形数】，不是索引元素数 —— 读端按
    # triangleCount*3 取索引。此前这里写的是 len(encoded_indices)（即元素数），
    # Cesium 于是去读 3 倍的索引而越界，抛 RangeError: Invalid typed array length
    # （tile_size=65 时 24576*3=73728），所有瓦片解码失败、地形一片都渲染不出来，
    # 且全程 HTTP 200、任务标 completed、前端不报错。
    # 下面 EdgeIndices 的 len(edge) 是对的：那几个字段 spec 定义就是顶点数。
    body.write(struct.pack("<I", len(encoded_indices) // 3))
    body.write(pack_indices(encoded_indices))

    for edge in edge_indices:
        body.write(struct.pack("<I", len(edge)))
        body.write(pack_indices(edge))

    # Oct-Encoded Per-Vertex Normals 扩展段（spec 的 ExtensionHeader:
    # unsigned char extensionId; unsigned int extensionLength;）。
    #
    # 追加在 EdgeIndices 之后、整条流的末尾，前面一个字节都不动 —— 于是
    # normals=None 时字节流与本函数长期以来的输出逐字节相同（golden 指纹不受
    # 影响）。扩展段不需要对齐 padding：Cesium 读它是 `new Uint8Array(buffer,
    # offset, vertexCount*2)`，Uint8Array 没有对齐要求（IndexData 那里要补是因为
    # Uint16Array/Uint32Array 有）。
    #
    # extensionLength 用小端：Cesium 读它是 `S.getUint32(a,
    # littleEndianExtensionSize)`，而 littleEndianExtensionSize 只有在 layer.json
    # 声明成老的 "vertexnormals" 时才是 false；我们声明的是 "octvertexnormals"，
    # 走小端。
    #
    # ⚠️ 长度校验不是防御性冗余：Cesium 取法线用的是 **vertexCount*2**，
    # 根本不看 extensionLength。多写了它静默只用前一截，少写了直接 RangeError
    # 整张瓦片解码失败 —— 前者正是本项目栽过五次的"什么都不报就是不对"。
    # 在编码端一次性挡掉，比让读端去猜强。
    if normals is not None:
        nrm = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
        if mesh is not None:
            nrm = nrm[mesh[0]]
        if len(nrm) != vertex_count:
            raise ValueError(
                f"normals count {len(nrm)} != vertex count {vertex_count}")
        oct_bytes = _oct_encode(nrm).tobytes()
        body.write(struct.pack("<BI", 1, len(oct_bytes)))
        body.write(oct_bytes)

    payload = header + body.getvalue()
    return payload


_WORKER_SAMPLER: DemSampler | None = None
# worker 进程内已创建过的 {z}/{x} 目录集合:每瓦片都 mkdir(parents=True,
# exist_ok=True) 是一次多余 syscall,去重后每目录只建一次。去重只在单次切片
# 任务内有效 —— _worker_init(并行:每 worker 进程一次;串行:_run_serial 调)
# 会清空,避免输出目录被删后同进程重跑时跳过 mkdir 导致写瓦片失败。
_WORKER_MKDIRS: set = set()


def _worker_init(input_path: str, nodata: float | None) -> None:
    global _WORKER_SAMPLER
    _WORKER_SAMPLER = DemSampler(input_path, nodata=nodata)
    _WORKER_MKDIRS.clear()


def _finite_or_none(value: float) -> float | None:
    """非有限值（±inf/NaN）归一成 None —— 写进 JSON 就是 null，而不是裸 Infinity。"""
    return float(value) if math.isfinite(value) else None


def _max_error_for_level(z: int, tile_size: int, k: float) -> float:
    """该层级的 max_error（米）。

    顶点间距 = 瓦片纬向跨度 / (tile_size-1)，换算成米后乘 K。固定绝对值不可用 ——
    实测 max_error=20m 时 z14 瓦片只剩 2 个三角形，整块压成平面。
    （地理切片方案下瓦片的经向跨度 360/2^(z+1) 与纬向跨度 180/2^z 恒等，
    所以 span_deg 对两个方向都成立。）

    DEG_TO_M 只是**量级换算**，不要理解成「坡度误差容限恒定」：它取的是赤道处
    1° 经度的度长（111320 m）。实测（tile_size=65）南北向由此得到的米数精确到
    0.2~0.7%，但东西向的真实顶点间距按 cos(纬度) 收缩，于是等效 K 按 1/cos(纬度)
    放大 —— lat41 → 0.199、lat52 → 0.243、lat70 → 0.437、lat80 → 0.862，
    约 62°N 以上东西向就越过了设计稿给 K 定的 0.3 上限。

    公式保持原样，因为影响很小且方向是「高纬更宽松」而非更严：同一份物理地形
    投到不同纬度，减面率 80.1%（赤道）→ 83.7%（80°），只差 3.6pp（边界满密度的
    地板占大头），本项目实际数据在 N28~N52，差 1~2pp。更重要的是**绝对高程
    容限与纬度无关**（z14 恒为 2.87 m），这条才是质量上的硬保证。
    """
    span_deg = 180.0 / (1 << z)
    spacing_m = span_deg / max(1, tile_size - 1) * DEG_TO_M
    return k * spacing_m


# compresslevel=6:quantized-mesh 是高熵二进制,9 级比 6 级压缩率提升可忽略但
# CPU 开销明显更大,瓦片量大时值得。实测把两个后端都升到 9 对择优比值最多动
# 0.008,救不了场,所以维持 6。
#
# mtime=0 是【必须】的,不是洁癖:gzip 头带 4 字节时间戳,gzip.open 写的是
# time.time(),于是同一份数据两次切片的磁盘字节不同。实测后果 —— 一条比较磁盘
# 字节的接线测试在干净树上连跑 8 次挂 1 次,差异恰好落在头部第 4 字节。逐瓦片
# 择优要拿两次压缩结果比大小、再把胜出的那份原样落盘,更是要求确定性。
# (顺带:gzip.compress 不写 FNAME 字段,而 gzip.open 会把文件名写进 gzip 头。
#  省下的是 len("{y}.terrain")+1 字节,随 y 的位数变 —— 实测 9.terrain 省 10、
#  12.terrain 省 11、373.terrain 省 12。这里以前写死"少 11 字节",不准。)
_GZIP_LEVEL = 6


def _gzip_tile(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=_GZIP_LEVEL, mtime=0)


def _choose_tile_bytes(mart_gz: bytes, grid_gz: bytes) -> Tuple[bytes, str]:
    """逐瓦片择优:取 **gzip 后**更小的那份,平局取 martini。

    为什么比的是 gzip 后:瓦片 gzip 落盘(见 _worker_tile)、terrain_static.py
    检测 gzip magic 后带 Content-Encoding: gzip 原样发出,所以 gzip 后的字节
    既是磁盘占用也是传输量,未压缩字节在生产里不代表任何东西。

    为什么需要择优(实测 112584 张配对瓦片,10 个真实 DEM):gzip 后规则网格
    ≈0.91 字节/三角形、martini ≈4.04 字节/三角形(贵 4.43 倍)—— 规则网格的
    u/v 是等差 zigzag-delta、索引是周期性 HWM 流,gzip 几乎白送;martini 打散后
    两者都成了高熵数据。于是**减面必须 >77.4% 才在字节上打平**,而山地只做到
    74.8%(gzip 后 +17.6% 净亏)、丘陵 75.2%(+9.8%),只有平缓地形 88.0%(-54.2%)
    是赚的。择优把变大的部分全部避免 => 全局净省 27.6%,且每张瓦片字节严格
    ≤ min(两者),**不可能变差**。调大 K 也能让 gzip 转正,但 K=0.30 实测会把
    整条沟谷拉成直线(结构性丢失),所以 K 保持 0.15 —— 质量完全不让步。

    平局取 martini:顶点更少,Cesium 侧内存与 GPU 上传更省。

    ⚠️ **判据是纯字节数,没有任何质量下限** —— "取更小的那个"读起来无条件安全,
    其实结构上偏向退化网格:一张塌掉的瓦片正因为塌掉了才字节最少,于是会被主动
    选中。实测在高程里注入一个 NaN,误差表被污染、martini 塌成 2 个三角形
    (gzip 后 65 字节) 对 grid 的 243 字节,auto **会选那张塌掉的**。
    这条路上唯一的闸门是 DemSampler.sample 的 NaN 兜底(它兜的是最终插值结果,
    不只是 nodata 替换,所以任何来源的 NaN 都拦得住)。谁要动那个兜底,
    先回来看这一段。
    """
    if len(mart_gz) <= len(grid_gz):
        return mart_gz, "martini"
    return grid_gz, "grid"


def _tile_normals(sampler: "DemSampler", west: float, south: float, east: float,
                  north: float, tile_size: int) -> np.ndarray:
    """一张瓦片的逐顶点法线 (tile_size, tile_size, 3)，用 ghost 环消跨瓦片接缝。

    ghost 环 = 向外各扩一个顶点步长采 (tile_size+2)²，算完法线裁回中间那圈。
    不扩环时边界顶点只能拿到本瓦片内的三角形，法线天生不完整，而且接缝**沿瓦片
    边界排成直线**（马赫带把它强化成可见网格，比同幅度的随机噪声显眼得多）。

    **归零是有条件的，条件在采样器上**。两侧邻接三角形集合一致 ⇒ 逐位相同，
    这一步只在 sampler 是「位置的纯函数」时成立。生产的 DemSampler.sample
    **不是**：读窗口由传入网格的 bbox 决定、再降采样到 buf = grid+2，相邻瓦片
    窗口不同 ⇒ 同一经纬点取到不同高程（实测同一批 65² 点，用 65² 窗口取 vs 用
    67² 窗口取再裁回，最大差 1.96 m）。于是：
      - 网格比源像素密、不发生降采样的层级 —— 天山 1″ DEM + tile_size=65 即
        z14（estimate_max_level 也正是 14）—— 实测 8/8 相邻瓦片边**逐位相同**；
      - 中间层只是大幅压小、不归零：z11 实测 199 对相邻瓦片边，边界法线夹角
        中位 2.42°→**0.238°**、最大 42.4°→3.27°；不扩环时 45.4% 的边界顶点
        Lambert 亮度阶跃超过 2% 可察觉阈值（86.4% 的瓦片对至少有一个顶点超）。
    ⚠️ 中间层的残差**不是法线的锅**：同一现象让瓦片自身的 65² 高程网格在公共边
    上就对不齐（实测 z9 差 35 m、z10–13 差 3~6 m、z14 差 0），几何裂缝先于法线
    接缝存在。属既有缺陷，不在本轮范围，但别把它误算成 ghost 环没生效。

    从 _worker_tile 里抽出来是为了可测：Task 6 阶段法线还没有消费者，内联时
    「ghost 不扩」「裁切下标写错」两种变异在任何测试下都是静默存活的（实测）。

    ⚠️ 已知限制：ghost 环落在 DEM 外时 DemSampler.sample 兜成 0，整个 DEM 最外
    一圈瓦片的最外一行顶点法线会朝向偏差（实测跨界瓦片最西列法线与地心方向
    夹角中位 3.70°）。这一条**确实不影响接缝** —— 相邻瓦片在同一地理位置取到
    同一个兜底 0：在采样器为纯函数的 z14 上实测天山 DEM 西边界 121 组边界瓦片
    对，0 组有差异。修它要让边缘顶点退化成只用内侧三角形，本轮不做。

    为什么不复用这里的采样结果去掉 _worker_tile 的那次满网格采样：GDAL 在低
    zoom 按读窗口降采样，67² 窗口与 65² 窗口取到的高程不同，复用会让瓦片几何
    随 normals 开关变化 —— 法线就从「附加数据」变成「改动既有产物」。
    """
    dw = (east - west) / (tile_size - 1)
    dh = (north - south) / (tile_size - 1)
    glon = np.linspace(west - dw, east + dw, tile_size + 2, dtype=np.float64)
    glat = np.linspace(south - dh, north + dh, tile_size + 2, dtype=np.float64)
    gl, ga = np.meshgrid(glon, glat)
    gh = sampler.sample(gl, ga)
    # 法线只喂广播轴（trig 算 n 次而非 n²，与 meshgrid 版逐位相同，见
    # _vertex_normals_ecef 的 docstring）；meshgrid 仍要给 sampler.sample 用。
    # 实测 n=67：_vertex_normals_ecef 0.580 ms → 0.508 ms（省 12.5%）。
    return _vertex_normals_ecef(glon[None, :], glat[:, None], gh)[1:-1, 1:-1]


def _worker_tile(task) -> Tuple[float, float, str] | None:
    """Returns (min, max, backend) of the written tile, or None on failure.

    backend ∈ {'martini','grid'} 是这张瓦片**实际落盘**的那个后端,由 build_terrain
    汇总成 chose_martini / chose_grid 计数 —— 排障时它直接告诉运维「这批 DEM 是
    什么地形」(全 grid = 山地,全 martini = 平缓)。

    逐瓦片容错:单个坏块(如 ReadAsArray 返回 None、磁盘写失败)只记日志计失败,
    不让一个瓦片炸掉整个任务 —— 与 contour 的 _render_contour_tile_core 同款。
    """
    (z, x, y, west, south, east, north, tile_size, out_dir,
     triangulator, max_error_k, with_normals) = task
    assert _WORKER_SAMPLER is not None

    try:
        # 瓦片边界（west/south/east/north）由调用方算好放进任务元组，worker 侧
        # 不需要 tiling scheme。
        lons = np.linspace(west, east, tile_size, dtype=np.float64)
        lats = np.linspace(south, north, tile_size, dtype=np.float64)
        llon, llat = np.meshgrid(lons, lats)
        heights = _WORKER_SAMPLER.sample(llon, llat)

        # 法线基于**满网格**算、两个三角化后端共用同一份：简化只动几何，光照
        # 细节保留（粗几何 + 精细法线，类似 normal mapping）。encode 那边传了
        # mesh 时会自己按保留顶点取子集。
        normals = _tile_normals(_WORKER_SAMPLER, west, south, east, north,
                                tile_size) if with_normals else None

        if triangulator in ("auto", "martini"):
            # 自适应三角化：按高程误差驱动细分，标准档（K=0.15）减面 73%~83%。
            # pin_border=True 是跨瓦片无缝的唯一保证，不要关。改成 False 实测：
            # 三角形塌 98.7%、公共边顶点数从满密度掉到 2~5、相邻瓦片出现真裂缝。
            # 守它的是 test_rtin.py 的接线测试（断言落盘瓦片四条边各 tile_size
            # 个顶点）—— 这行以前是零守卫的，关掉全量测试一条不红。
            # 惰性 import：rtin 只依赖 numpy，但放在这里可以让「切 grid 回退」
            # 在 rtin 出问题时是真的完全绕开它。
            from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract
            err = rtin_errors(heights.reshape(-1), tile_size, pin_border=True)
            mesh = rtin_extract(err, tile_size, _max_error_for_level(z, tile_size, max_error_k))
        else:
            mesh = None

        if triangulator == "auto":
            # 两个后端各编一遍、各压一遍，取小者。多出来的成本是第二次编码 +
            # 第二次 gzip（rtin_errors 本身已向量化，不是大头）。
            # **两条分支都要传 normals**：只传一条的话，另一条胜出的瓦片就没有
            # 法线段，而 layer.json 照样声明 octvertexnormals、HTTP 照样 200、
            # Cesium 照样不报错 —— 只是那些瓦片不受光照。
            mart_gz = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                       heights, mesh=mesh,
                                                       normals=normals))
            grid_gz = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                       heights, mesh=None,
                                                       normals=normals))
            blob, backend = _choose_tile_bytes(mart_gz, grid_gz)
        else:
            blob = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                    heights, mesh=mesh,
                                                    normals=normals))
            backend = triangulator

        out_path = Path(out_dir) / str(z) / str(x)
        if out_path not in _WORKER_MKDIRS:
            out_path.mkdir(parents=True, exist_ok=True)
            _WORKER_MKDIRS.add(out_path)
        (out_path / f"{y}.terrain").write_bytes(blob)
        return float(np.min(heights)), float(np.max(heights)), backend
    except Exception as e:
        logger.warning(f"terrain 瓦片生成失败 z={z} x={x} y={y}: {e!r}")
        return None


def build_terrain(
    inputs: list[str],
    output_dir: str,
    *,
    min_level: int = 0,
    max_level: int | None = None,
    tile_size: int = 17,
    nodata: float | None = None,
    workers: int = 0,
    progress_cb=None,
    stage_cb=None,
    stop_flag=None,
    triangulator: str = "auto",
    max_error_k: float = DEFAULT_MAX_ERROR_K,
    normals: bool = True,
    level_offset: int = 0,
) -> dict:
    # progress_cb(done, total): 逐瓦片进度回调（done = rendered+failed，terrain
    # 没有 contour 的 skipped 态），串行/并行每 tally 一次调一次；调用方自行
    # 节流（范本：contour_engine.build_contour_tiles）。
    #
    # stage_cb(phase, fraction): 瓦片循环**之前**那些耗时阶段的进度，phase ∈
    # {'merge','overview'}，fraction 是 0..1。为什么不能复用 progress_cb：多幅
    # 输入要先物化成单文件（实测 6 幅 13.6 秒、大任务分钟级），而那发生在 total
    # 算出来**之前** —— total 要靠 sampler，sampler 要靠物化产物，物理上没有分母。
    # 调用方同样要自行节流：GDAL 的原生回调频率不受这里控制。
    #
    # stop_flag: threading.Event
    # 式协作停止 —— 串行每瓦片检查、并行批间检查；置位后提前收尾（已生成的
    # 瓦片保留，layer.json/meta.json 照常按已处理部分写出）。
    #
    # triangulator: 'auto' = 逐瓦片择优（默认，两个后端都编一遍取 gzip 后更小的，
    # 见 _choose_tile_bytes 里的实测依据），'martini' / 'grid' = 强制单一后端，
    # 排障时用。max_error_k: 允许高程误差 = K * 顶点间距（见 _max_error_for_level）。
    # 三者都不暴露给 UI/DB/API，只为排障与测试注入而存在。
    #
    # normals: 逐顶点法线（ECEF 空间 + ghost 环，见 _worker_tile / _vertex_normals_ecef），
    # 落盘成 oct-encoded 扩展段（每顶点 2 字节，见 encode_quantized_mesh 末尾），
    # 同时决定下面 layer.json 的 extensions 字段。默认开。
    #
    # 成本（天山 N42E086，z0-11 共 907 张，tile_size=65，workers=4，交错 best-of-4）：
    # 墙钟 0.932s → 1.266s，**+35.8%**。其中约 28pp 是采样+叉积（Task 6 在同一
    # 口径下测的 +27.7%，那时法线还没有消费者），其余是多编一段 + 多压一次。
    # 字节代价随后端差得很远（4 个真实 DEM、z8-12、3120 张配对瓦片，gzip 后总量）：
    # **grid +55.4%、martini 只 +22.5%** —— 法线字节正比于顶点数，而 grid 恒是
    # 4225 个顶点、martini 中位 589~1751 个。于是逐瓦片择优的平衡整体倒向 martini：
    #   天山 martini 325→**467** / 黄土 38→**187** / 华北 725→730 / 荷兰 621→639，
    #   3120 张里 314 张从 grid 翻成 martini，**反向翻转 0 张**。
    # 「auto 不可能比任一单后端差」不受影响（3120 张实测 0 处违反）。
    # ⚠️ 用 build_terrain 直接做实验时必须显式传 tile_size=65 —— 本函数默认值是
    # 17，生产走的是 TileParams.tile_size=65，两者的择优落点完全不同
    # （ts=17 时 martini 减面空间太小，实测张张选 grid）。
    #
    # 下面这两条校验在入口一次性做掉，不放进 _worker_tile —— 那里的 try/except
    # 是逐瓦片容错，配置错在那里只会变成每张瓦片一行 warning：
    #   - triangulator 拼错（'martni'）会静默走 else 分支退回规则网格，作业
    #     rendered==total 完美完成，没有任何信号说自适应根本没开（实测）。
    #   - tile_size 不是 2^k+1 时 rtin_tables 抛 ValueError，实测 10/10 瓦片
    #     全失败。生产路径恒为 65（TileParams.tile_size）不受影响，但 CLI 的
    #     --tile-size 收任意整数，且默认开自适应之后这是【新引入】的失败面 ——
    #     改成入口即报，错误直指病因而不是刷 N 行瓦片级 warning。
    #
    # 'auto' 在 tile_size 上与 'martini' 一样严，**不静默降级成纯 grid**：
    # 静默降级正是这个项目栽过三次的失败形态（作业 completed、前端不报错、
    # 什么都不显示）。生产默认 tile_size=65 是合法的，能触发这条就说明配置错了，
    # 应该当场暴露而不是悄悄退化成一个字节更大的产物。
    if triangulator not in ("auto", "martini", "grid"):
        raise ValueError(
            f"triangulator must be 'auto', 'martini' or 'grid', got {triangulator!r}")
    if triangulator in ("auto", "martini"):
        t = int(tile_size) - 1
        if t < 2 or (t & (t - 1)) != 0:
            raise ValueError(
                f"triangulator={triangulator!r} requires tile_size = 2^k+1 and >= 3, "
                f"got {tile_size} (use triangulator='grid' for arbitrary sizes)")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 物化产物落在输出目录旁边而不是系统临时目录：它和源数据同量级（6 幅 ASTER
    # 约 92 MB，大任务可到 GB），/tmp 在部分部署里是内存盘，塞不下。
    input_path = build_input_raster(inputs, work_dir=str(out.parent),
                                    stage_cb=stage_cb)
    # 多输入时 build_input_raster 落在 tempfile.mkstemp 的临时文件，此前从不删除，
    # 每次多输入切片泄漏一个临时文件（M18）。删除时机必须在进程池消费完之后
    # （worker 在 initializer 里各自打开这个文件），所以挂在整个切片过程的
    # finally 上，best-effort 删除。
    temp_input = input_path if input_path not in inputs else None
    try:
        sampler = DemSampler(input_path, nodata=nodata)
        scheme = GeographicTilingScheme(tile_size=tile_size)

        # 基准层级：显式传了就用它，否则按源像素尺寸估算。
        if max_level is None:
            max_level = scheme.estimate_max_level(sampler.pixel_size_deg)
        # 档位偏移叠加在基准上（精度 +1 / 均衡 0 / 速度 -1，见
        # docs/reference/terrain/tiling-presets-measured.md）。实测每加一级约
        # 3.3 倍体积换 2.8 倍精度，与源分辨率无关。
        # 钳位到 [0, MAX_ZOOM]：负层级会让 _tile_ranges 的 range() 直接空转，
        # 越界层级会让瓦片数按 4^n 爆炸。
        # 注意这一行对 level_offset=0 **不是**恒等变换 —— max_level 自身越界时
        # 它照样收紧，也就是说从不传偏移的既有调用方同样受影响。这是有意的：
        # dem_task_manager.py:310 在 maxzoom=None 时用裸 int() 读配置
        # terrain_local_maxzoom，绕过 validate_zoom（该键登记在
        # config_manager.py:308 的 _UNCONSTRAINED_KEYS 里），配置写 25 就能一路
        # 到这里切出 z25；而 local_terrain_task_manager.py:149 那条路径是有
        # validate_zoom 的 —— 两条入口本就不对称。封顶 21 是堵这个缺口。
        max_level = max(0, min(MAX_ZOOM, int(max_level) + int(level_offset)))
        # 调用方钳不到偏移后的实际层级。dem_task_tiler 恒传 min_level=8（底图独占
        # z0-z7），它连**请求的** maxzoom 都不看 —— maxzoom < 8、或 maxzoom = 8
        # 叠上 -1 偏移把 max_level 压到 7 时，都会出现 min_level > max_level。
        # min_level > max_level 会让 _tile_ranges 产出空区间 —— 切 0 张瓦片却报
        # completed，本仓栽过的同款静默成功。最终层级只有这里知道，所以这道钳位
        # 删不得，也没法挪到调用方。
        min_level = min(int(min_level), max_level)

        src_w, src_s, src_e, src_n = sampler.bounds

        def _tile_ranges():
            # compute intersected tile ranges per level
            for z in range(min_level, max_level + 1):
                nx, ny = scheme.tile_count(z)
                ix0, ix1, iy0, iy1 = intersecting_tile_range(
                    nx, ny, src_w, src_s, src_e, src_n)

                if z <= 4:
                    # 低 zoom 仍然出图（根瓦片缺失会让 Cesium 的单层 provider
                    # 路径直接 404），但 available 只声明真正相交的那部分 ——
                    # 见下方 available_per_level 的注释。
                    x0, x1, y0, y1 = 0, nx - 1, 0, ny - 1
                else:
                    x0, x1, y0, y1 = ix0, ix1, iy0, iy1
                yield z, x0, x1, y0, y1, ix0, ix1, iy0, iy1

        available_per_level = []
        # Count tiles arithmetically (no list) — materializing every tile task tuple
        # OOMs at high max_level over large areas (same pitfall contour fixed).
        total = 0
        for z, x0, x1, y0, y1, ix0, ix1, iy0, iy1 in _tile_ranges():
            if x1 < x0 or y1 < y0:
                available_per_level.append([])
                continue
            # M12: available 只声明与 DEM **真正相交** 的瓦片范围（ix*），而不是
            # 实际出图的整个矩形（x*）。z<=4 时后者被强制成全球，于是每个任务都
            # 会声明「我有全球 z0-4 地形」—— 而那些瓦片的采样窗口落在 DEM 之外，
            # 返回全 0 被兜成 hmin=0/hmax=1 的平面，或者被边缘像素钳位拉成台地
            # （实测东半球 z0 瓦片 hmin=913 hmax=2502 m）。
            # Cesium 1.143 的 requestTileGeometry 取「第一个 availability 声明可用
            # 的层」，而父层是在子层【之后】才追加的 —— 于是 parentUrl 指向的
            # base_z8 的 z0-4 永远不会被请求，整个低层被这些假地形盖掉。
            # 只收窄声明、仍照常出图：base 不存在时单层 provider 不查 availability，
            # 根瓦片仍能拿到文件，不会退化成「地形完全不加载」。
            a0, a1, b0, b1 = max(x0, ix0), min(x1, ix1), max(y0, iy0), min(y1, iy1)
            if a1 < a0 or b1 < b0:
                available_per_level.append([])
            else:
                available_per_level.append(
                    [{"startX": a0, "startY": b0, "endX": a1, "endY": b1}])
            total += (x1 - x0 + 1) * (y1 - y0 + 1)

        # total 到这里就已知了，而 progress_cb 要等**第一张瓦片编完**才发出第一
        # 报 —— 大任务的第一张可能要好几秒，期间前端只能显示 0/0。先上报一次。
        if progress_cb is not None:
            try:
                progress_cb(0, total)
            except Exception:
                logger.debug("progress_cb(0, total) failed", exc_info=True)

        def _iter_tasks():
            for z, x0, x1, y0, y1, _ix0, _ix1, _iy0, _iy1 in _tile_ranges():
                if x1 < x0 or y1 < y0:
                    continue
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        west, south, east, north = scheme.tile_extent_deg(z, x, y)
                        yield (z, x, y, west, south, east, north, tile_size, str(out),
                               triangulator, max_error_k, normals)

        sampler.ds = None
        sampler.band = None

        t0 = time.time()
        h_min_global = float("inf")
        h_max_global = float("-inf")
        done = 0
        failed = 0
        # 逐瓦片择优的落点统计。排障价值：全 grid = 这批 DEM 是山地/粗糙地形，
        # 全 martini = 平缓地形；两者混合才是 auto 在干活。强制单一后端时全部
        # 落在对应那一侧，可用来确认「切 grid 回退」真的切过去了。
        chose = {"martini": 0, "grid": 0}
        workers = int(workers or 0)
        if workers <= 0:
            # 保守默认:每个 worker 都要打开 GDAL dataset 并逐瓦片读 DEM 窗口,worker
            # 太多内存吃紧;无脑 cpu_count 在多核机器上可能把内存打爆、worker 被 OS
            # 杀掉(BrokenProcessPool)。封顶 4,与 contour 一致;显式传 workers 可覆盖。
            workers = min(4, os.cpu_count() or 1)

        def _emit() -> None:
            if progress_cb is not None:
                # done 按 processed(rendered+failed) 计:失败瓦片也是处理完的
                # 瓦片,不计的话进度条会停在 <100% 就结束(同 contour_engine._emit)。
                progress_cb(done + failed, total)

        def _tally(result) -> None:
            nonlocal h_min_global, h_max_global, done, failed
            if result is None:
                failed += 1
                _emit()
                return
            mn, mx, backend = result
            h_min_global = min(h_min_global, mn)
            h_max_global = max(h_max_global, mx)
            chose[backend] += 1
            done += 1
            _emit()

        def _run_serial() -> None:
            _worker_init(input_path, nodata)
            for task in _iter_tasks():
                # 串行:stop_flag 每瓦片即时检查(同 contour 的 _render_serial)。
                if stop_flag is not None and stop_flag.is_set():
                    break
                _tally(_worker_tile(task))

        if workers == 1 or total <= 4:
            _run_serial()
        else:
            # 并行:worker 在 initializer 里各自打开 dataset。瓦片按 batch 从生成器取:
            # Executor.map 会把传入 iterable 一次性全部 submit 成 Future,直接接生成器
            # 等于变相物化整张任务列表,高 max_level 大区域会 OOM(同 contour 的坑)。
            from concurrent.futures import ProcessPoolExecutor
            from concurrent.futures.process import BrokenProcessPool
            BATCH = 512  # 批内并行,批间检查 stop_flag;不物化整张瓦片列表。
            # 2048 -> 512(同 contour_engine):停止/暂停要等当前批跑完才生效,
            # 批小 4 倍响应约快 4 倍;单批 list 内存本身可忽略。
            # 显式 spawn，不用平台默认。Linux 的默认是 fork，而这里的父进程是
            # 多线程的 Flask（CPython 3.12 起就为此打 DeprecationWarning「use of
            # fork() may lead to deadlocks in the child」：子进程只继承调用线程，
            # 父进程里被别的线程持有的锁在子进程里永远解不开）。三条理由：
            #   1. Windows / macOS 的打包产物本来就走 spawn —— 那是主要交付平台，
            #      也就是说 worker 早已必须是 spawn-safe（模块级函数、initargs 全
            #      可 pickle），Linux 走 fork 只是让三平台行为不一致；
            #   2. Python 3.14 会把 Linux 默认改成 forkserver，迟早要面对；
            #   3. contour_engine 的池已经是 spawn（同一批改的），两处不该分叉。
            # 代价：每个 worker 要重新 import numpy/GDAL，池创建慢几秒。池在一个
            # 切片作业里只建一次，相对整轮切片可忽略。
            from multiprocessing import get_context
            try:
                with ProcessPoolExecutor(max_workers=workers,
                                         mp_context=get_context("spawn"),
                                         initializer=_worker_init,
                                         initargs=(input_path, nodata)) as ex:
                    tasks = _iter_tasks()
                    while True:
                        if stop_flag is not None and stop_flag.is_set():
                            break
                        batch = list(itertools.islice(tasks, BATCH))
                        if not batch:
                            break
                        for result in ex.map(_worker_tile, batch, chunksize=8):
                            _tally(result)
            except BrokenProcessPool as e:
                # worker 进程被异常终止(多 worker 内存耗尽被 OS 杀常见)。进程级崩溃
                # except 兜不住,会让整个任务失败 -> 回退串行重跑保证切片完整:已生成
                # 的 .terrain 被覆盖,未生成的补齐。串行单进程内存压力小,通常能跑完。
                # 计数清零重跑后 progress_cb 会从 0 重新上报(同 contour 的回退),
                # 调用方的时间节流天然兼容,最终计数由收尾覆盖,不依赖逐次累计。
                logger.warning(f"并行切片 worker 崩溃({e!r}),回退串行重跑整个任务")
                h_min_global = float("inf")
                h_max_global = float("-inf")
                done = 0
                failed = 0
                chose["martini"] = 0
                chose["grid"] = 0
                _run_serial()

        # 收尾日志：择优落点是排障时最直接的一条信息 —— 全 grid 说明这批 DEM
        # 是山地/粗糙地形（martini 在 gzip 后反而更大），全 martini 说明是平缓
        # 地形。没有这行，chose_* 计数只有直接调 build_terrain 的人看得到。
        logger.info(
            f"terrain 切片完成 triangulator={triangulator} total={total} "
            f"rendered={done} failed={failed} chose_martini={chose['martini']} "
            f"chose_grid={chose['grid']} 用时 {time.time() - t0:.1f}s")

        layer_name = Path(inputs[0]).stem if len(inputs) == 1 else f"{Path(inputs[0]).stem}+{len(inputs)-1}"
        layer = {
            "tilejson": "1.0",
            "name": layer_name,
            "description": "",
            "version": "1.0.0",
            "format": "quantized-mesh-1.0",
            "attribution": "",
            "scheme": "tms",
            "tiles": ["{z}/{x}/{y}.terrain"],
            "projection": "EPSG:4326",
            "bounds": [-180, -90, 180, 90],
            "valid_bounds": list(sampler.bounds),
            "minzoom": min_level,
            "maxzoom": max_level,
            "available": available_per_level,
            # 不声明的话法线写了也是白写：Cesium 的 CesiumTerrainProvider 拿这个
            # 数组决定 layer.hasVertexNormals，而 requestTileGeometry 只有在
            # `_requestVertexNormals && hasVertexNormals` 时才把 extensions 放进
            # Accept 头、解析时也只有这时才去取 OCT_VERTEX_NORMALS 段。声明成老的
            # "vertexnormals" 会把 extensionLength 切成大端读，别写混。
            "extensions": ["octvertexnormals"] if normals else [],
        }
        (out / "layer.json").write_text(json.dumps(layer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        meta = {
            "minLevel": min_level,
            "maxLevel": max_level,
            # 全部瓦片都失败时（rendered==0 且 total>0）两个哨兵值一次都没被
            # 收窄，仍是 ±inf，而 json.dumps 默认把它们写成裸 Infinity ——
            # 不是合法 JSON，任何严格解析器（浏览器 JSON.parse、Go、Rust）读
            # meta.json 直接报错。归一成 null 而不是用 allow_nan=False：后者
            # 会在所有瓦片都已落盘之后抛 ValueError，把「切了但没切出东西」
            # 变成异常，counts 丢失、上层看不到 rendered/failed，正是 P1#7
            # 那种「记账失败作废产物」。null 是可解析的「无高度范围」，与
            # rendered==0 的判失败逻辑正交。
            "minHeight": _finite_or_none(h_min_global),
            "maxHeight": _finite_or_none(h_max_global),
            "bounds": list(sampler.bounds),
            "tileSize": tile_size,
            "scheme": "EPSG:4326",
        }
        (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # 计数结构对照 contour 的 build_contour_tiles(无 skipped:terrain 没有跳过态)。
        # chose_martini + chose_grid == rendered:每张成功瓦片恰好落在一个后端上。
        return {"total": total, "rendered": done, "failed": failed,
                # 实际切到的最深层级。档位偏移后它可能不等于调用方传进来的
                # max_level，调用方要据此落库/展示，不能拿请求值当产物事实。
                "max_level": max_level,
                "chose_martini": chose["martini"], "chose_grid": chose["grid"]}
    finally:
        # 串行路径（workers==1 / total<=4 / BrokenProcessPool 回退）在**本进程**
        # 里跑 _worker_init，留下的全局 sampler 一直握着源 DEM 的 GDAL dataset 和
        # 块缓存直到进程退出。此前这段挂在 `if temp_input:` 下，而单输入 tif 时
        # temp_input 恒为 None（见上面那行），于是串行路径必漏：Windows 上源文件
        # 被占，删除任务时 rmtree(ignore_errors=True) 静默失败，UI 报删除成功而
        # 上传的 tif 还在盘上。置 None 而不只清 ds/band —— 不把释放时机压在 GC 上，
        # 也让下一次 _worker_init 从干净状态开始。
        global _WORKER_SAMPLER
        if _WORKER_SAMPLER is not None:
            _WORKER_SAMPLER.ds = None
            _WORKER_SAMPLER.band = None
            _WORKER_SAMPLER = None
        if temp_input:
            # 当前路径下 overview 是**内嵌**的：GA_Update 打开 GTiff 再
            # BuildOverviews 会写进 .tif 本身（实测 GetFileList 只返回一个文件）。
            # 所以下面 .ovr / .aux.xml 两条纯属防御 —— 改成只读模式打开、或换个
            # 驱动，GDAL 才会另写外部边车。变异测试里删掉它们不会让任何用例变红，
            # 这是如实的：它们当前没有可观察行为。
            for path in (temp_input, temp_input + ".ovr", temp_input + ".aux.xml"):
                if not os.path.exists(path):
                    continue
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning(f"Failed to remove temp input {path}: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DEM -> Cesium quantized-mesh-1.0 terrain tiles (cesiumlab compatible)")
    ap.add_argument("--input", "-i", required=True, action="append")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--min-level", type=int, default=0)
    ap.add_argument("--max-level", type=int, default=None)
    ap.add_argument("--tile-size", type=int, default=17)
    ap.add_argument("--nodata", type=float, default=None)
    ap.add_argument("--workers", "-j", type=int, default=0)
    # 排障开关：'auto' 逐瓦片择优（默认，与生产一致），'martini'/'grid' 强制
    # 单一后端做对比。auto/martini 要求 tile_size = 2^k+1（默认 17 满足），
    # 传 64 之类会在 build_terrain 入口报错并指向这个 flag。
    ap.add_argument("--triangulator", choices=("auto", "martini", "grid"), default="auto")
    ap.add_argument("--max-error-k", type=float, default=DEFAULT_MAX_ERROR_K)
    args = ap.parse_args(argv)

    import glob

    inputs: list[str] = []
    for spec in args.input:
        if os.path.isdir(spec):
            for ext in ("*.tif", "*.tiff", "*.img", "*.hgt", "*.vrt"):
                inputs.extend(sorted(glob.glob(os.path.join(spec, ext))))
            # 排掉自己的中间产物：物化件就落在 work_dir（= 输出目录的父级）,
            # 而 `-i <目录>` 常常指向同一片区域。SIGKILL 留下的残留会被当成
            # 一幅 DEM 重新吃进去 —— 它是**上一次全部输入的合并结果**,再合一次
            # 等于把整片地形叠加两遍。DEM 任务侧躲过这一劫只是因为
            # list_dem_tifs 匹配的是 *_dem.tif / *_DEM.tif。
            inputs = [p for p in inputs
                      if not os.path.basename(p).startswith("cesiumlab_terrain_")]
        elif any(c in spec for c in "*?["):
            inputs.extend(sorted(glob.glob(spec)))
        else:
            inputs.append(spec)
    inputs = [p for p in inputs if os.path.isfile(p)]
    if not inputs:
        sys.exit("no input files matched")

    build_terrain(
        inputs,
        args.output,
        min_level=args.min_level,
        max_level=args.max_level,
        tile_size=args.tile_size,
        nodata=args.nodata,
        workers=args.workers,
        triangulator=args.triangulator,
        max_error_k=args.max_error_k,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
