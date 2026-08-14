"""GeoTIFF 产物 → GeoPackage 栅格导出。

GPKG 驱动可用性在 **export() 里** 判，不在 import 时判。理由是用户看到的东西
不一样：模块级判定失败会变成 registry 里的一条 `load_error`，插件整个不可用、
导出格式列表里连 `gpkg` 都没有，用户读到的是「插件坏了」；而事实是「这个 GDAL
构建不带 GPKG 驱动」。放到导出时判，插件照常载入，只有真按下导出时才给出
「GPKG 驱动不可用（当前 GDAL 构建不带）」这句明白话——打包产物（Nuitka）裁掉
驱动时，这是用户唯一能看懂的线索，而不是一段 ImportError 栈。

产物 kind 复用 `GEOTIFF`：GPKG 在语义上仍是「单文件栅格数据集」，靠 `fmt='gpkg'`
区分。`ArtifactKind` 的值即落库文本，不为一种导出格式扩枚举。
"""

from __future__ import annotations

from pathlib import Path

from src.core.gdal_mode import pin_gdal_exception_mode

# osgeo import 之前先钉死本进程的 GDAL 异常模式：export() 里的两道判错闸门
# （返回值 `is None` + CPL 错误栈）都以**非异常模式**为前提，GDAL 4.0 把默认
# 翻成异常模式后它们会变成死代码。理由见 src/core/gdal_mode.py 的 docstring。
pin_gdal_exception_mode()

from osgeo import gdal  # noqa: E402

from src.contracts.artifact import Artifact, ArtifactKind  # noqa: E402
from src.core.database import utc_now_iso  # noqa: E402
from src.plugins.protocols import ExportContext, PluginDefinition  # noqa: E402

MANIFEST = {
    'id': 'gpkg',
    'name': 'GeoPackage 导出',
    'version': '1.0.0',
    'api_version': '1',
    'capabilities': ['exporter'],
    'permissions': ['filesystem'],
    'description': '把 GeoTIFF 产物导出为 GeoPackage 栅格（GPKG）。',
}


class GpkgExporter:
    """`Exporter` 协议实现：单个 GeoTIFF → 单个 .gpkg。"""

    def format_id(self) -> str:
        return 'gpkg'

    def accepts(self, kind) -> bool:
        return kind is ArtifactKind.GEOTIFF

    def export(self, artifact: Artifact, dest: Path,
               ctx: ExportContext) -> Artifact:
        if gdal.GetDriverByName('GPKG') is None:
            raise RuntimeError('GPKG 驱动不可用（当前 GDAL 构建不带）')
        ctx.log(f'导出 {artifact.path} → {dest}')
        ctx.progress(0, 1)

        # ExceptionMgr 守临界区：四条流水线共用一个 Flask 进程，contour_engine
        # 会无条件调 gdal.UseExceptions()（进程全局），那之后 CE_Failure 直接抛
        # Python 异常、不回填 CPL 错误栈，下面那道 GetLastErrorType 闸门就永远
        # 读到 0。与模块顶部的 pin 不重复：那个定进程默认值，这个守这一段。
        with gdal.ExceptionMgr(useExceptions=False):
            gdal.ErrorReset()
            ds = gdal.Translate(str(dest), artifact.path, format='GPKG')
            # 三道闸门，一道都不能省。**`ds is not None` 不等于写成功**：实测
            # 磁盘满 / 配额 / 超 4 GiB 时 gdal.Translate 照样返回非 None 的
            # dataset、尺寸照样对，只有数据被截断（见
            # tests/test_fix_gdal_silent_failure_gaps.py 与
            # cesium_terrain._raise_on_gdal_error）。静默交出坏产物的代价是用户
            # 拿着一份打得开、读回来是空的 GPKG，而导出报成功。
            if ds is None:
                raise RuntimeError(
                    f'gdal.Translate 失败：{artifact.path} → {dest}')
            ds.FlushCache()
            ds = None       # Windows 上不放手连文件都删不掉，写盘也要靠它落地
            if gdal.GetLastErrorType() >= gdal.CE_Failure:
                raise RuntimeError(
                    f'gdal.Translate 报成功，但 GDAL 记了一条失败：{dest}：'
                    f'{gdal.GetLastErrorMsg()!r}（GDAL error '
                    f'{gdal.GetLastErrorNo()}）')

        # 最后一次 flush 才失败时 GDAL 连错误记录都不留，上面那道闸门读不到。
        # 体积为 0（或文件根本不在）是这种情况下唯一还能自己查的信号。
        try:
            bytes_total = dest.stat().st_size
        except OSError as e:
            raise RuntimeError(f'导出产物不存在：{dest}（{e}）') from e
        if bytes_total <= 0:
            raise RuntimeError(f'导出产物是空文件：{dest}')

        ctx.progress(1, 1)
        return Artifact(
            pipeline=artifact.pipeline, task_id=artifact.task_id,
            kind=ArtifactKind.GEOTIFF, path=str(dest), fmt='gpkg',
            bytes_total=bytes_total,
            minzoom=artifact.minzoom, maxzoom=artifact.maxzoom,
            has_gaps=artifact.has_gaps,
            meta={'exported_from': str(artifact.path)},
            created_at=utc_now_iso())


def register() -> PluginDefinition:
    return PluginDefinition(exporters=(GpkgExporter(),))
