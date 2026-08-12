"""`artifacts` 表的读写。产物登记的唯一入口。

改造前「这个任务产出了什么」这件事没有事实来源：产物位置以字符串形式散在
任务行的 `output_path` / `output_dir` 上，目录命名规则在四条 DELETE 路由、
四个静态服务蓝图和四个管理器里各写了一遍，而一个任务同时产出 XYZ 目录、
每层 GeoTIFF 与 MBTiles 时（§5.3 明确要求支持）没有任何地方能列出它们。

登记表的两条硬约定，都写在 `database.py` 的建表注释里，这里复述结论：

1. **没有外键。** 产物行必须能比任务行活得久 —— 用户「删任务、留文件」时它
   是文件还在的唯一线索，「删任务、删文件」时它是后台清理线程的工作清单。
   挂 CASCADE 就等于在最需要它的那一刻把它删掉。代价是登记与任务行可能不同步
   （任务没了、产物行还在），所以 `list_artifacts` 的调用方不能假设任务存在。
2. **`INSERT OR REPLACE` 幂等。** 唯一键是 `(pipeline, task_id, kind, path)`。
   同一产物重跑（续传、补漏、重新导出）会更新字节数与瓦片数，不产生第二行。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, List, Optional

from src.contracts.artifact import Artifact, ArtifactKind
from src.core.database import get_connection, utc_now_iso

logger = logging.getLogger(__name__)

__all__ = [
    'record_artifact',
    'record_artifacts',
    'list_artifacts',
    'delete_artifacts_for',
    'measure_dir',
]


def measure_dir(root, *, extensions: Optional[Iterable[str]] = None) -> tuple:
    """目录产物的规模统计 → `(bytes_total, file_count, minzoom, maxzoom)`。

    层级从 `{z}/{x}/{y}` 布局的一级目录名推断；目录不是瓦片金字塔时两个
    层级都是 None。统计失败（权限、目录已被删）返回全零，不抛 —— 登记产物
    是收尾动作，不该把一个已经成功的任务翻成失败。
    """
    root = Path(root)
    total = 0
    count = 0
    zooms = []
    try:
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                zooms.append(int(entry.name))
        for path in root.rglob('*'):
            try:
                if not path.is_file():
                    continue
                if extensions and path.suffix.lower() not in extensions:
                    continue
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
    except OSError as e:
        logger.warning(f'产物规模统计失败（{root}）：{e!r}')
        return 0, 0, None, None
    return total, count, (min(zooms) if zooms else None), (max(zooms) if zooms else None)


def record_artifact(artifact: Artifact) -> Optional[int]:
    """登记一件产物。返回行 id；失败返回 None 并记警告，**不抛**。

    不抛的理由同 `measure_dir`：登记是收尾动作。一次登记失败最坏是缓存管理
    页少列一行，让它把一个已经成功的下载任务打成失败是完全不成比例的。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT OR REPLACE INTO artifacts (
                pipeline, task_id, kind, path, format, bytes_total, tile_count,
                minzoom, maxzoom, has_gaps, meta, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            artifact.pipeline, artifact.task_id, artifact.kind.value,
            str(artifact.path), artifact.fmt, artifact.bytes_total,
            artifact.tile_count, artifact.minzoom, artifact.maxzoom,
            1 if artifact.has_gaps else 0, artifact.meta_json(),
            artifact.created_at or utc_now_iso(),
        ))
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        conn.rollback()
        logger.warning(f'产物登记失败（{artifact.pipeline}/{artifact.task_id} '
                       f'{artifact.kind.value}）：{e!r}')
        return None
    finally:
        conn.close()


def record_artifacts(artifacts: Iterable[Artifact]) -> int:
    return sum(1 for a in artifacts if record_artifact(a) is not None)


def list_artifacts(pipeline: str, task_id: int) -> List[Artifact]:
    """某任务的全部产物，登记顺序。任务行可能已经不存在（见模块 docstring）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM artifacts WHERE pipeline = ? AND task_id = ? ORDER BY id',
            (pipeline, int(task_id))).fetchall()
    except Exception as e:
        logger.warning(f'产物查询失败（{pipeline}/{task_id}）：{e!r}')
        return []
    finally:
        conn.close()

    out = []
    for row in rows:
        try:
            out.append(Artifact.from_row(row))
        except (ValueError, KeyError) as e:
            # 一行坏数据（比如手工改过 kind）不该让整个详情页 500 ——
            # 与 Task.from_row 绕过校验是同一条理由。
            logger.warning(f'跳过无法解析的产物行 id={row["id"]}：{e!r}')
    return out


def delete_artifacts_for(pipeline: str, task_id: int) -> int:
    """删掉某任务的产物登记（**不删文件**）。

    只在「任务行与磁盘产物一起删」的那条路径上调用。用户选择保留文件时
    **不要**调用它 —— 那一行正是「文件还在」的唯一线索。
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            'DELETE FROM artifacts WHERE pipeline = ? AND task_id = ?',
            (pipeline, int(task_id)))
        conn.commit()
        return cur.rowcount or 0
    except Exception as e:
        conn.rollback()
        logger.warning(f'产物登记删除失败（{pipeline}/{task_id}）：{e!r}')
        return 0
    finally:
        conn.close()
