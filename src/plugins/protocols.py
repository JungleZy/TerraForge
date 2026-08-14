"""四类扩展点的协议与共享数据类。

刻意不进 src/contracts/：contracts 的不变量是「零 Flask / 零 GDAL / 零
SQLite import，测试与估算器可脱离 app 使用」，而这里的行为协议恰恰服务
于运行期装配。数据形状（RegionSpec/SourceSnapshot/Artifact/TileOutcome）
继续从 contracts 拿，这里不重复定义。

插件作者只看这一个文件 + task_context.py 的 TaskContext。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, Mapping, Optional, Protocol,
                    Sequence, Tuple, runtime_checkable)

#: 宿主导出的插件 API 版本。major 不匹配的插件拒载——第三方插件在宿主升级后
#: 要明确坏掉，而不是静默行为错乱。
PLUGIN_API_VERSION = '1.0'
API_MAJOR = PLUGIN_API_VERSION.split('.')[0]


@dataclass(frozen=True)
class SourceDescriptor:
    """一个插件数据源的声明。纯数据——多数源不需要一行代码。"""

    source_id: str
    name: str                       # 显示名（插件自带文案，不进主 catalog）
    url_template: str               # 必须含 {z}{x}{y}；可选 {s} / {credential}
    max_zoom: int
    attribution: str = ''
    usage_policy: str = ''
    subdomains: Tuple[str, ...] = ()
    credential_key: str = ''        # plugins.config_json 里的键名；'' = 无凭据


@dataclass(frozen=True)
class ParamSpec:
    """声明式任务参数。type ∈ region|zoom_range|path|int|float|str|bool|enum|credential。"""

    key: str
    type: str
    label: str
    default: Any = None
    required: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Tuple[str, ...] = ()
    depends_on: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParamSchema:
    specs: Tuple[ParamSpec, ...] = ()

    def keys(self) -> Tuple[str, ...]:
        return tuple(s.key for s in self.specs)


class PluginOutcome(Enum):
    """PipelinePlugin.run() 的返回。completed_with_gaps / pending_decision
    触发宿主走 §13-3 的缺块决策流，插件自己不写这套状态机。"""

    COMPLETED = 'completed'
    COMPLETED_WITH_GAPS = 'completed_with_gaps'
    PENDING_DECISION = 'pending_decision'


@dataclass(frozen=True)
class TaskEvent:
    """钩子事件。v1 的 kind 只有 'task_completed'（规格 §14：核心管线的
    状态迁移统一成事件源是后续独立工作）。"""

    kind: str
    pipeline: str                   # v1 恒 'plugin'
    task_id: int
    plugin_id: str


@dataclass(frozen=True)
class ExportContext:
    task_id: int
    log: Callable[[str, str], None]          # (message, level)
    progress: Callable[[int, int], None]     # (done, total)


@runtime_checkable
class SourceProvider(Protocol):
    """需要代码鉴权/动态元数据的数据源才实现它；纯模板源只用 SourceDescriptor。"""

    def list_sources(self) -> Sequence[SourceDescriptor]: ...
    def snapshot(self, source_id: str, cfg: Mapping[str, str]): ...  # → SourceSnapshot
    def authorize(self, headers: Dict[str, str], cfg: Mapping[str, str]) -> None: ...


@runtime_checkable
class PipelinePlugin(Protocol):
    def params_schema(self) -> ParamSchema: ...
    def estimate(self, params: Mapping[str, Any], region) -> Any: ...  # → DiskEstimate
    def run(self, ctx) -> PluginOutcome: ...                          # ctx: TaskContext


@runtime_checkable
class Exporter(Protocol):
    def format_id(self) -> str: ...
    def accepts(self, kind) -> bool: ...          # kind: ArtifactKind
    def export(self, artifact, dest: Path, ctx: ExportContext): ...   # → Artifact


@runtime_checkable
class TaskHook(Protocol):
    """旁路：宿主调用时包 try/except，抛异常只记日志，绝不影响任务。"""

    def on_event(self, event: TaskEvent) -> None: ...


@dataclass(frozen=True)
class PluginDefinition:
    """plugin.py 的 register() 返回值。全部成员可选——纯数据源插件只有 sources。"""

    sources: Tuple[SourceDescriptor, ...] = ()
    source_provider: Optional[SourceProvider] = None
    pipeline: Optional[PipelinePlugin] = None
    exporters: Tuple[Exporter, ...] = ()
    hooks: Tuple[TaskHook, ...] = ()
