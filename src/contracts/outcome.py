"""TileOutcome / TaskState —— 瓦片级结果与任务状态机。

## 瓦片结果

改造前瓦片级结果是**临时的裸字符串**：`download_engine` 产出
`'completed' / 'failed' / 'cancelled'`，只有 `'failed'` 会落进 `task_tiles`
（`task_manager.py` 里一个硬编码字面量），而「404 没覆盖」和「网络挂了」
和「缓存写不进去」在库里逐位相同。结果是任务终态没法解释：一个 failed
任务，用户无从知道是自己框到了海里，还是代理断了。

GeoDownloader 也是裸字符串（`downloader.rs:24-31` 的
`"completed_with_no_data"` / `"completed_with_errors"`，再在
`commands.rs:829` 重映射）—— 它正好反证了结构化的必要性。

五个取值（§4.4）：

    success            拿到了合法瓦片
    no_data            上游明确说这里没有数据（404 / 空覆盖）。**这是被解释的缺块**
    retryable_failure  网络错误、超时、5xx、429 —— 重跑可能成功
    permanent_failure  4xx（除 429）、响应不是图片且重试耗尽 —— 重跑也不会成功
    cache_failure      瓦片拿到了但写不进缓存（磁盘满 / 权限）。**最危险的一类**：
                       缓存文件就是完成态的事实来源，写失败却报成功会产出
                       「completed 但缺文件」且永远无法自愈的任务

只有 success 不落库；其余四种作为稀疏行留在 `task_tiles`，行的存在即缺块。

## 任务状态机

    pending / running / paused / retrying / pending_decision
              / completed_with_gaps / completed / failed

新增三个的理由：

- `retrying` —— 补漏（gap fill）重跑期间的状态。没有它，补漏和首次下载在
  历史里长得一样，用户分不清「这任务跑了三遍」。
- `pending_decision` —— 有缺块、需要用户决定「补漏 / 部分导出 / 放弃」。
  §13-3 已决「允许显式导出部分成果」，那么必须有一个状态承载「等你决定」，
  否则默认严格（不产出成品）就等于静默卡住。
  GeoD 把 `PendingDecision` 刻意排在 `Paused` 之后的主流程里
  （`task.rs:19-20` 的注释），为的是让暂停/恢复无法把它伪装回 Downloading ——
  这里用同样的办法：`start_task` 的准入白名单不含它，只有专门的决策端点能出去。
- `completed_with_gaps` —— 有缺块但用户已显式接受。**不是静默成功**：
  成果与历史永久带缺块标记（§11 明确列为「不采纳」的反例）。

`cancelled` 不回来。它在 `database.migrate_cancelled_tasks_to_failed` 里
已经迁成 `failed`，目标状态机里也没有它 —— 用户主动删除的任务在历史里
本来就不该留行（删除即删行），所以不需要这个状态。
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet

__all__ = [
    'TileOutcome',
    'TaskState',
    'ACTIVE_TASK_STATES',
    'TERMINAL_TASK_STATES',
    'RESUMABLE_TASK_STATES',
    'SUCCESSFUL_TASK_STATES',
    'ACTIVE_STATE_VALUES',
    'TERMINAL_STATE_VALUES',
    'RESUMABLE_STATE_VALUES',
    'SUCCESSFUL_STATE_VALUES',
    'GAP_OUTCOMES',
    'RETRYABLE_OUTCOMES',
    'is_gap_outcome',
    'outcome_from_db',
    'LEGACY_FAILED_OUTCOME',
]


class TileOutcome(Enum):
    """一块瓦片的结局。值即落库文本。"""

    SUCCESS = 'success'
    NO_DATA = 'no_data'
    RETRYABLE_FAILURE = 'retryable_failure'
    PERMANENT_FAILURE = 'permanent_failure'
    CACHE_FAILURE = 'cache_failure'

    @property
    def is_success(self) -> bool:
        return self is TileOutcome.SUCCESS

    @property
    def is_gap(self) -> bool:
        """算不算「成品上的一个洞」。success 之外全算。"""
        return self is not TileOutcome.SUCCESS

    @property
    def is_explained(self) -> bool:
        """缺块是否已被上游解释清楚。

        只有 `no_data` 是：上游明确回答了「这里没有数据」。其余三种都是
        「我们没拿到，原因在我们这边或网络上」—— 那种任务不能算完成。
        """
        return self in (TileOutcome.SUCCESS, TileOutcome.NO_DATA)


#: 会在 `task_tiles` 里留行的结局。
GAP_OUTCOMES: FrozenSet[TileOutcome] = frozenset(
    o for o in TileOutcome if o is not TileOutcome.SUCCESS)

#: 补漏时值得重试的结局。`permanent_failure` 不在其中 —— 重试 4xx 只是浪费配额。
#: `no_data` 也不在：上游说过没有，再问一遍还是没有。
RETRYABLE_OUTCOMES: FrozenSet[TileOutcome] = frozenset({
    TileOutcome.RETRYABLE_FAILURE,
    TileOutcome.CACHE_FAILURE,
})

#: 存量 `task_tiles` 行的取值。user_version=5 的迁移会把它改写成
#: `retryable_failure`（保守方向：宁可让用户多重试一次，也不要把一个其实
#: 能救回来的瓦片标成永久失败）。留常量是给迁移和测试引用。
LEGACY_FAILED_OUTCOME = 'failed'


def outcome_from_db(value) -> TileOutcome:
    """落库文本 → TileOutcome。未知值（含存量 `'failed'`）按可重试处理。

    不抛异常：一行坏数据不该让整个历史页 500。
    """
    if isinstance(value, TileOutcome):
        return value
    try:
        return TileOutcome(str(value))
    except ValueError:
        return TileOutcome.RETRYABLE_FAILURE


def is_gap_outcome(value) -> bool:
    return outcome_from_db(value).is_gap


class TaskState(Enum):
    """任务状态。值即落库文本，四张任务表共用。"""

    PENDING = 'pending'
    RUNNING = 'running'
    PAUSED = 'paused'
    RETRYING = 'retrying'
    PENDING_DECISION = 'pending_decision'
    COMPLETED = 'completed'
    COMPLETED_WITH_GAPS = 'completed_with_gaps'
    FAILED = 'failed'

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_TASK_STATES

    @property
    def is_active(self) -> bool:
        """「还在系统手里」—— 活动列表、缓存清理阻断、退出前确认都看它。"""
        return self in ACTIVE_TASK_STATES

    @property
    def is_successful(self) -> bool:
        """产出可用（可能带洞）。预览、后续处理、成品服务看它。"""
        return self in (TaskState.COMPLETED, TaskState.COMPLETED_WITH_GAPS)

    @property
    def has_gaps(self) -> bool:
        return self in (TaskState.COMPLETED_WITH_GAPS, TaskState.PENDING_DECISION)


#: 正在跑或随时可能继续跑。`pending_decision` **在内**：它占着产物目录、
#: 占着缓存引用，清缓存时必须被拦住，退出前必须提示。
ACTIVE_TASK_STATES: FrozenSet[TaskState] = frozenset({
    TaskState.PENDING,
    TaskState.RUNNING,
    TaskState.PAUSED,
    TaskState.RETRYING,
    TaskState.PENDING_DECISION,
})

#: 不会再自己变了。
TERMINAL_TASK_STATES: FrozenSet[TaskState] = frozenset({
    TaskState.COMPLETED,
    TaskState.COMPLETED_WITH_GAPS,
    TaskState.FAILED,
})

#: `start_task` / `resume_task` 的准入白名单。
#:
#: 刻意**不含** `pending_decision`：它必须走专门的决策端点（补漏 / 接受缺块），
#: 否则一次误点「继续」就把「等你决定」洗成了普通 running，用户再也看不到
#: 这个任务需要决策。形制照抄 GeoD `task.rs:19-20` 的注释，理由完全相同。
#:
#: 也不含 `failed`：改造前就是终态，`start_task` 明确拒绝。
RESUMABLE_TASK_STATES: FrozenSet[TaskState] = frozenset({
    TaskState.PENDING,
    TaskState.PAUSED,
})

#: 「产出可用」的终态。历史筛选与统计的「已完成」必须是**这两个**，不是
#: `completed` 一个。
#:
#: 实测过的后果：`GET /api/history_all` 对非 active 的筛选值走
#: `WHERE status = ?`，而界面上的筛选芯片只有 全部 / 进行中 / 失败 / 已完成，
#: 于是一个 `completed_with_gaps` 的任务**任何一个芯片都匹配不上**（只有「全部」
#: 能看到它），`history_stats.completed` 也把它漏掉。用户下完一批带缺块但完全
#: 可用的成果，去「已完成」里找，找不到 —— 而 §13-3 的整个前提是「允许显式
#: 导出部分成果」，找不到就等于那条产品决定白做了。
SUCCESSFUL_TASK_STATES: FrozenSet[TaskState] = frozenset({
    TaskState.COMPLETED,
    TaskState.COMPLETED_WITH_GAPS,
})


def _values(states) -> tuple:
    return tuple(sorted(s.value for s in states))


#: 给 SQL 用的字面量元组。`status IN (...)` 的正向清单是本仓约定
#: （从不写反向清单），这些常量保证四条管线用的是同一份。
ACTIVE_STATE_VALUES = _values(ACTIVE_TASK_STATES)
TERMINAL_STATE_VALUES = _values(TERMINAL_TASK_STATES)
RESUMABLE_STATE_VALUES = _values(RESUMABLE_TASK_STATES)
SUCCESSFUL_STATE_VALUES = _values(SUCCESSFUL_TASK_STATES)
