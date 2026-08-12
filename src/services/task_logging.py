"""每任务日志与诊断（§4.5）—— 给每个任务一个独立的、有尺寸与保留期上限的日志文件。

## 为什么需要它

改造前只有一个全局 sink：`<BASE_DIR>/logs/terraforge.log`（见
`src/core/logging_setup.py`，按天轮转保留 7 天）。任务身份是靠 f-string 前缀
夹在消息里传的（`f"DEM tiling job {task_id}: ..."`），于是排查一个任务要在一
个被四条管线 + werkzeug 瓦片访问日志共同刷写的文件里 grep 一个数字，而那个
数字还会撞上其他含同一数字的行。更糟的是保留期只有 7 天且按**天**轮转：一个
跑了两小时、失败在第 90 分钟的任务，它的解释在一次轮转之后就和别的任务混在
同一份归档里。

本模块解决的是「任何终态都能从日志解释原因」（§4.5 的门槛）：
数据库仍是任务的**事实源**（状态、计数、产物），日志是**解释源**（为什么变
成这个状态）。两者职责不重叠 —— 所以这里既不写库也不读任务表。

## 两个 sink，故意都要

`TaskLogger` 包的 logger 叫 `task.<pipeline>.<task_id>` 且 `propagate=True`：
每任务文件是**追加**的 sink，不是替代。全局日志与控制台照旧收到每一行（而且
现在 record.name 天然带上了任务身份，比 f-string 前缀更可靠）。把 propagate
关掉的诱惑很大（控制台会安静很多），但那等于让任务日志在用户看不见的地方发生
——「跑着没反应」时第一反应是看控制台，不是去翻 logs/tasks/。

## 关于级别：跟随 LOG_LEVEL，但有一条 INFO 地板

task logger 的级别取 `min(根 logger 的有效级别, INFO)`：

- 不能直接设 DEBUG。logging 的传播规则是**只在发起调用的那个 logger 上检查
  级别**，传播到祖先时只再过 handler 的级别，不再过祖先 logger 的级别。给
  task logger 设 DEBUG 就等于把 DEBUG 行直接灌进控制台和全局文件（它们的
  handler 都没设级别），`LOG_LEVEL=INFO` 会变成一句空话。
- 也不能直接留 NOTSET（纯继承）。根 logger 在没跑过 `configure_logging` 的
  进程里（测试、脚本、以库形式被 import）默认是 WARNING，那样 `tlog.info()`
  会被整条丢掉，任务日志文件根本不会出现 —— 而「文件是空的」和「任务没打
  日志」在事后完全分不清。

所以取两者的下界：默认 `LOG_LEVEL=INFO` 时两个 sink 一致；调到 DEBUG 时两个
sink 一起变详细（这个联动是有意的）；调到 WARNING/ERROR 想让控制台安静时，
任务级 INFO 仍然会出现在控制台 —— 这是明知的取舍：§4.5 的门槛是「任何终态
都能从日志解释原因」，一个因为 LOG_LEVEL 被调高而空白的任务日志直接违背它，
而任务级 INFO 的行数量级（每阶段几行）不构成刷屏。

## 目录是懒创建的，永远不在 import 期

`tests/test_no_repo_pollution.py:73-93` 钉住「跑测试期间仓库根目录不得出现
logs/」。所以本模块 import 时不碰文件系统，`open_task_log()` 也不 mkdir ——
目录在 `_TaskFileHandler._open()` 里、也就是**第一条记录真的要落盘**的那一刻
才建（handler 用 `delay=True`）。只 import 或只开一个 logger 却从不写日志的
进程（测试、脚本）不留任何痕迹。`.gitignore` 已经整个挡住 `logs/`，无需改动。

## 一切都不抛异常

每一个公开方法都吞掉自己的异常。理由不是「日志不重要」，而是这些方法会被塞进
**GDAL 的进度回调**里：`progress_cb` 抛出会被 GDAL 当成用户主动中止，于是它
**删掉已经写了一大半的输出文件**（同一条踩过的坑记在
`src/services/dem_task_manager.py:499-508` 与
`src/services/terrain_tiling/cesium_terrain.py:555-563`）。一次 ENOSPC、一次
只读目录、一次 Windows 上的 PermissionError，不该有作废一个 99% 完成的产物的
权力。落盘失败只在模块自己的 logger 上警告一次（见 `_warn_once`）—— 每条记录
警告一次会把全局日志刷爆，而那正是磁盘已经满了的时候。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import sys
import time
from pathlib import Path

from src.contracts.artifact import PIPELINES
# _ANSI_ESCAPE 是有意 import 的私有名：werkzeug 把色码塞在**消息内容里**，
# 而两份 ANSI 正则迟早漂移 —— 那时表现是全局日志干净、诊断包里一堆
# `^[[33m`，没人会想到是两份正则的差异。宁可耦合一个私有名。
from src.core.logging_setup import _ANSI_ESCAPE, log_dir
from src.services.system_proxy import mask_url_userinfo

logger = logging.getLogger(__name__)

__all__ = [
    'REDACTED',
    'REDACTION_PATTERNS',
    'TASK_LOG_DIR_NAME',
    'TaskLogger',
    'diagnostics_text',
    'open_task_log',
    'prune_task_logs',
    'read_task_log',
    'redact',
    'task_log_dir',
    'task_log_path',
]

#: 每任务日志所在的子目录名（在全局 logs/ 之下）。与 LOG_DIR_NAME 一样从代码读，
#: 手抄字面量的话改了常量而 .gitignore / 清理逻辑照旧匹配旧名。
TASK_LOG_DIR_NAME = 'tasks'

#: 日志文件名形制：`<pipeline>_<task_id>.log`，RotatingFileHandler 的备份是
#: `<...>.log.1`。prune 与列目录都用这一条判定「这是我们的文件」——
#: 不加锚定的话 terraforge.log 本体或用户手放的文件会被当成任务日志删掉。
_LOG_NAME_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(p) for p in PIPELINES) + r')_-?\d+\.log(?:\.\d+)?$')

#: 反向读取的块大小。4 MB 的日志取 500 行不该把整个文件读进内存。
_TAIL_CHUNK = 64 * 1024

#: 落盘格式。比全局日志窄一截：logger 名已经在文件名里了（`map_12.log`），
#: 每行再重复一遍 `task.map.12` 是纯噪声，还把 4 MB 的额度花在冗余上。
_FILE_FORMAT = '%(asctime)s %(levelname)s %(message)s'

#: 解析回结构化条目用。asctime 默认形如 `2026-08-12 10:20:30,123`；
#: 匹配不上的行（多行回溯的续行）挂到上一条的 message 上，不单独成条。
_ENTRY_RE = re.compile(
    r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[.,]\d+)?)\s+([A-Z]+)\s+(.*)$')

#: `errors_only` 认的级别。WARNING 也算进来：重试、429、无覆盖这些**恰好**
#: 是 WARNING，而它们正是「为什么只下到一半」的答案。只留 ERROR 的话
#: 「只看错误」这个开关在最常见的排查场景下会给出一片空白。
_ERROR_LEVELS = frozenset({'WARNING', 'ERROR', 'CRITICAL'})

#: task logger 级别的地板：再高也不会高过 INFO（见模块 docstring「关于级别」）。
#: 没有它，未跑过 configure_logging 的进程里根 logger 是 WARNING，
#: `tlog.info()` 全被丢掉，任务日志文件根本不会出现。
_LEVEL_FLOOR = logging.INFO

REDACTED = '***'

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

# 匹配文本里嵌着的 URL。redact 不能假设入参**整体**是一个 URL：它拿到的是
# 一整行日志（`fetch failed: https://u:p@h/x?token=... -> 500`）。
_URL_IN_TEXT = re.compile(r'\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\'"<>)\]},]+')

#: 敏感键名。前缀故意开放（`[\w\-]*`）：`access_key` / `api_key` /
#: `earthdata_password` / `X-Auth-Token` 都要盖住，而 `\bkey` 这种写法会因为
#: 下划线是 word 字符而**漏掉** `access_key=`。宁可多盖（`cache_key=abc` 也
#: 会变成 `***`）—— 少盖一次就是一次凭据泄漏，多盖一次只是一行少了点信息。
#:
#: 开头那个 `(?<![\w\-])` 不是语义修饰，是**复杂度**修饰，删掉就是一个 DoS：
#: 没有它，`[\w\-]*` 会在一个长 token 的**每一个偏移**上重新往后扫一遍，
#: 整体退化成 O(n^2) —— 实测 40,000 字符的连续 token 光这一条规则就要 35.6 秒，
#: 128 KB 一行端到端 353 秒。而日志里出现一个几十 KB 不带空白的 token
#: （一段 base64、一条超长 URL、一个 GDAL 抛出来的巨型 WKT）再正常不过。
#: 加上它之后，只有「词首」才是候选起点，同一个 token 只会被扫一遍。
_SENSITIVE_KEY = (
    r'(?<![\w\-])[\w\-]*'
    r'(?:password|passwd|token|key|secret|authorization|signature|credential)')

#: 脱敏规则清单。测试可见：`(名字, 正则, 替换)`。名字用于断言「这条规则还在」，
#: 而不是去 assert 一整段实现 —— 规则会增加，删掉一条才是回归。
#: 顺序有意义：URL userinfo 先做（它整段重写 URL），再做 `k=v` 与头部，
#: 最后才替换 home 路径（提前做会把 URL 里的路径也换掉）。
REDACTION_PATTERNS = (
    # scheme://user:pass@host —— 由 _redact_urls 走 mask_url_userinfo 处理，
    # 这里登记的是它的匹配范围，规则实现不在正则里（见下方 _redact_urls）。
    ('url_userinfo', _URL_IN_TEXT, None),
    # Bearer / Basic 后面那一整段。头名字可能被截断（日志里常只剩
    # `Bearer eyJ...`），所以认的是**方案词**而不是头名字。
    ('auth_scheme_value', re.compile(
        r'(?i)\b(Bearer|Basic|Token)\s+([A-Za-z0-9\-._~+/=]{4,})'), r'\1 ' + REDACTED),
    # query 参数与形如 `token='abc'` 的 kwargs / dict 字面量。
    # 值的终止符里带上 `'"`,;)}]&`：不带的话会一路吃到行尾，把后面的
    # 有用信息（状态码、耗时）一起吞掉，日志就没法用了。
    ('key_eq_value', re.compile(
        r'(?i)([\'"]?' + _SENSITIVE_KEY + r'[\'"]?\s*=\s*)[\'"]?[^\s&\'",;)}\]]+'),
     r'\1' + REDACTED),
    # HTTP 头的冒号形式：`Authorization: Bearer x`、`{'Cookie': 'a=b'}`。
    # 值到逗号/花括号/行尾为止 —— dict 字面量里逗号就是下一项的开始。
    # 这条只认几个**固定头名**，因为它允许值里带空格（`Bearer x` 是两段）。
    ('header_colon_value', re.compile(
        r'(?i)([\'"]?(?:authorization|proxy-authorization|cookie|set-cookie|'
        r'x-api-key|x-auth-token|x-amz-security-token)[\'"]?\s*:\s*)'
        r'[\'"]?[^,;}\r\n]+'),
     r'\1' + REDACTED),
    # 通用的 `敏感键: 值` 形式。上面那条只覆盖一张固定的 HTTP 头名单，而
    # 冒号形式在日志里最常见的来源根本不是 HTTP 头，是**结构化数据的 repr**：
    # 上游回的 JSON 错误体（`{"password": "Hunter2"}`）、Python 的 dict/dataclass
    # repr、YAML 片段。这些一样会进 `diagnostics_text`，而那是给用户导出去
    # 发给别人的文件 —— 少盖一条就是把密码发出去了。
    # 值的终止符与 `key_eq_value` 一致（不吃空格、不跨逗号），并且把闭合引号
    # 一起吃掉，避免留下 `"password": ***"` 这种孤零零的尾引号。
    ('sensitive_colon_value', re.compile(
        r'(?i)([\'"]?' + _SENSITIVE_KEY + r'[\'"]?\s*:\s*)'
        r'[\'"]?[^\s&\'",;)}\]]+[\'"]?'),
     r'\1' + REDACTED),
)


def _redact_urls(text: str) -> str:
    """把文本里每个 URL 的 `user:pass@` 掩码掉。

    复用 `system_proxy.mask_url_userinfo`（掩成 `***:***@host`，host 保留便于
    排查）而不是再写一条正则：userinfo 的边界判定（IPv6 字面量里的冒号、
    路径里的 `@`）已经在那边靠 urlsplit 解决过一次，抄第二遍必然抄漏。
    """
    def _one(m):
        try:
            return mask_url_userinfo(m.group(0))
        except Exception:
            # mask 失败宁可整段丢掉也不能原样吐出 —— 这段里可能就是密码。
            return REDACTED
    return _URL_IN_TEXT.sub(_one, text)


def _home_path_variants():
    """用户家目录的所有书写形式，长的在前（先替换长的，短的才不会截断长的）。

    §4.5 要求「本地敏感路径必须脱敏」：家目录里带真实姓名 / 工号是常态，
    而诊断包是要发给别人看的。
    """
    try:
        home = str(Path.home())
    except Exception:
        return ()
    # 长度门槛防的是退化情形：某些容器里 HOME='/' 或 '',替换它等于把整段
    # 路径全打成 '~',日志直接失去意义。
    if len(home) < 4:
        return ()
    variants = {home, home.replace('\\', '/'), home.replace('/', '\\')}
    return tuple(sorted(variants, key=len, reverse=True))


def redact(text: str) -> str:
    """去掉凭据与本地敏感路径。展示、复制、导出之前**都**要过这一道。

    盖住：URL 里的 `user:pass@`、`Bearer/Basic/Token <值>`、含敏感词的
    `k=v`（query 参数、kwargs、dict 字面量）、Authorization / Cookie 等头的
    冒号形式，以及用户家目录（换成 `~`）。规则清单见 `REDACTION_PATTERNS`。

    逐条 try：一条正则在某种畸形输入上炸掉（灾难性回溯被 re 内部保护，但
    替换函数可能抛）不该让**其余规则也不生效**，那会从「少盖一条」退化成
    「一条都没盖」。所以失败时保留已经处理过的中间结果继续往下走，绝不
    fallback 回原文 —— 原文正是那个带密码的字符串。
    """
    try:
        out = text if isinstance(text, str) else str(text)
    except Exception:
        return REDACTED
    try:
        out = _redact_urls(out)
    except Exception:
        pass
    for name, pattern, replacement in REDACTION_PATTERNS:
        if replacement is None:
            continue  # url_userinfo 由 _redact_urls 负责，见清单里的注释
        try:
            out = pattern.sub(replacement, out)
        except Exception as e:
            _warn_once(f'redact:{name}', '脱敏规则 %s 执行失败（已跳过）：%s', name, e)
    for home in _home_path_variants():
        try:
            out = out.replace(home, '~')
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def task_log_dir() -> Path:
    """每任务日志目录 `<BASE_DIR>/logs/tasks`。

    父目录走 `logging_setup.log_dir()` 而不是自己拼 `Config.BASE_DIR / 'logs'`：
    打包模式下 BASE_DIR 是 exe 所在目录（见 core/config.py），这条规则已经在
    那边解释过一次；两处各拼一遍的话，改了全局日志位置而任务日志留在旧地方，
    表现是「日志目录里只有一半的日志」。

    只算路径，不建目录 —— 目录在第一条记录落盘时才建（见模块 docstring）。
    """
    return log_dir() / TASK_LOG_DIR_NAME


def task_log_path(pipeline: str, task_id: int) -> Path:
    """`<BASE_DIR>/logs/tasks/<pipeline>_<task_id>.log`。

    两个参数都校验，因为它们**直接变成文件名**：pipeline 必须是
    `contracts.artifact.PIPELINES` 里的一条（白名单，只此一份），task_id 强制
    int()。不校验的话一个 `'../../data/map_downloader'` 就能让日志 handler 去
    覆盖数据库文件 —— 这不是理论风险，pipeline 与 task_id 都从 HTTP 请求来。

    Raises:
        ValueError: pipeline 不在白名单，或 task_id 不是整数（路由层统一
            catch ValueError → HTTP 400）。
    """
    if pipeline not in PIPELINES:
        raise ValueError(f'未知的管线标识：{pipeline!r}（可选：{", ".join(PIPELINES)}）')
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise ValueError(f'任务 ID 必须是整数：{task_id!r}') from None
    return task_log_dir() / f'{pipeline}_{tid}.log'


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def _config_value(key: str, config_manager=None) -> str:
    """读一个配置项，读不到就回退 DEFAULT_CONFIGS 里的出厂值。

    ConfigManager 与 DEFAULT_CONFIGS 都**局部** import：本模块会被 GDAL 回调、
    清理线程这些地方 import，而 ConfigManager 会顺带把 database + geo_validation
    整条链拉起来。日志模块不该有这种 import 期依赖。

    兜底值从 DEFAULT_CONFIGS 现取而不是手抄字面量 —— 兜底和出厂默认不一致
    会造出「改了没反应」的假旋钮（同 `local_terrain_task_manager` 里那条规矩）。

    ConfigManager.get 对 sqlite 错误是**有意重抛**的（见 config_manager.py:431），
    而全新克隆尚无 data/ 目录时它必抛；日志功能不能因此瘫掉，所以这里兜住。
    """
    try:
        from src.core.database import DEFAULT_CONFIGS
        default = dict(DEFAULT_CONFIGS).get(key, '')
    except Exception:
        default = ''
    try:
        cm = config_manager
        if cm is None:
            from src.services.config_manager import ConfigManager
            cm = ConfigManager()
        value = cm.get(key, default)
    except Exception as e:
        _warn_once(f'config:{key}', '读取配置 %s 失败，按出厂值 %r 处理：%s', key, default, e)
        return default
    return default if value is None else str(value)


def _int_config(key: str, config_manager=None) -> int:
    """整数配置项；非法值退回出厂默认并警告一次（绝不抛）。"""
    raw = _config_value(key, config_manager)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        try:
            from src.core.database import DEFAULT_CONFIGS
            fallback = int(dict(DEFAULT_CONFIGS)[key])
        except Exception:
            fallback = 0
        _warn_once(f'int:{key}', '配置 %s 的值 %r 不是整数，按 %d 处理', key, raw, fallback)
        return fallback


def _bool_config(key: str, config_manager=None) -> bool:
    """布尔配置项。库里存的是 'true'/'false' 字面量（见 DEFAULT_CONFIGS）。"""
    return _config_value(key, config_manager).strip().lower() == 'true'


# ---------------------------------------------------------------------------
# 一次性告警
# ---------------------------------------------------------------------------

_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    """同一类失败只在**模块自己的** logger 上警告一次。

    每条记录都警告会在「磁盘满 / 目录只读」时把全局日志刷爆 —— 而那正是最
    需要全局日志还能看的时候。用模块 logger 而不是 task logger：task logger 的
    handler 正是坏掉的那个，往它写就是递归。
    """
    if key in _warned:
        return
    _warned.add(key)
    try:
        logger.warning(msg, *args)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handler / Formatter
# ---------------------------------------------------------------------------

class _TaskFileFormatter(logging.Formatter):
    """`时间 级别 消息`，并剥掉一切 ANSI 色码。

    与 `logging_setup.PlainFormatter` 同一个理由（色码来自消息内容而不是我们
    自己上色），但格式更窄：logger 名已经在文件名里，不再重复。
    """

    def __init__(self):
        super().__init__(fmt=_FILE_FORMAT)

    def format(self, record):
        return _ANSI_ESCAPE.sub('', super().format(record))


class _TaskFileHandler(logging.handlers.RotatingFileHandler):
    """按尺寸轮转的每任务文件 handler；建目录与失败静默都在这里。

    - `_open()` 里 mkdir：这样目录在**第一条记录真的落盘**时才出现，
      import 与 open_task_log 都不碰文件系统（见模块 docstring 里的
      test_no_repo_pollution 约束）。
    - `handleError()` 全吞：默认实现在 `logging.raiseExceptions` 为真时往
      stderr 打一段 `--- Logging error ---` 回溯，只读目录下**每条记录**都会
      打一次，终端直接不可用；而这个 handler 的失败已经由 _warn_once 汇报过。
    """

    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()

    def handleError(self, record):
        _warn_once(f'emit:{self.baseFilename}',
                   '每任务日志写入失败（本次运行不再重复报告）：%s', self.baseFilename)


class TaskLogger:
    """一个任务的日志句柄。所有方法都不抛异常。

    ⚠️ 为什么每个方法都吞异常：本类的方法会被塞进 GDAL 的进度回调
    （`progress_cb`）。回调抛出会被 GDAL 当成用户主动中止，于是它**删掉已经
    写了一大半的输出文件** —— 同一条坑记在
    `src/services/dem_task_manager.py:499-508` 与
    `src/services/terrain_tiling/cesium_terrain.py:555-563`。一次 ENOSPC 或
    Windows 上的文件锁不该有作废一个 99% 完成的产物的权力。

    底层是名为 `task.<pipeline>.<task_id>` 的标准 logger，`propagate=True`：
    每任务文件是**追加**的 sink，全局日志与控制台照旧收到每一行。
    """

    def __init__(self, pipeline: str, task_id: int):
        self.pipeline = pipeline
        self.task_id = task_id
        # 文件 handler 由 open_task_log 装上（要先摘掉同 id 的遗留 handler，
        # 所以不能从构造参数进来）。这里为 None 就是「只写全局日志」那一档。
        self._handler = None
        # logger 对象是进程级缓存的，所以级别每次都要显式写回：别的代码
        # （或上一轮同 id 的任务）设过级别的话会一直留着。
        # 地板与「为什么不是 DEBUG、也不是 NOTSET」见模块 docstring「关于级别」。
        self._logger = logging.getLogger(f'task.{pipeline}.{task_id}')
        self._logger.setLevel(min(logging.getLogger().getEffectiveLevel(), _LEVEL_FLOOR))
        self._logger.propagate = True

    # -- 路径 ---------------------------------------------------------------

    @property
    def path(self):
        """本任务日志文件路径；日志被关闭（task_log_enabled=false）时为 None。"""
        if self._handler is None:
            return None
        return Path(self._handler.baseFilename)

    @property
    def enabled(self) -> bool:
        """是否有独立文件在收。False 时消息仍然进全局日志。"""
        return self._handler is not None

    # -- 记录 ---------------------------------------------------------------

    def _log(self, level: int, msg, args) -> None:
        try:
            self._logger.log(level, msg, *args)
        except Exception:
            # 连 logger.log 都炸（Formatter 遇到坏的 %s 参数会）——
            # 这里绝不能再往任何 logger 写，见类 docstring。
            pass

    def debug(self, msg, *args) -> None:
        self._log(logging.DEBUG, msg, args)

    def info(self, msg, *args) -> None:
        self._log(logging.INFO, msg, args)

    def warning(self, msg, *args) -> None:
        self._log(logging.WARNING, msg, args)

    def error(self, msg, *args) -> None:
        self._log(logging.ERROR, msg, args)

    def exception(self, msg, *args) -> None:
        """ERROR + 当前回溯。except 块里用这个，不然「为什么失败」只剩一句话。"""
        try:
            self._logger.error(msg, *args, exc_info=True)
        except Exception:
            pass

    def event(self, kind: str, **fields) -> None:
        """一行结构化事件：`EVENT <kind> k=v k=v`。

        状态机的每次转换、调度器的每次配额分配、磁盘预算的每次重估、
        SourceSnapshot 的 fingerprint、瓦片 outcome 的分类计数都走这里 ——
        §4.5 要求「任何终态都能从日志解释原因」，而自由文本没法事后统计。

        含空白或 `=` 的值走 repr（加引号），否则 `note=disk full` 会被解析成
        两个字段、第二个还没有名字。字段顺序按调用顺序，不排序：调用方把最
        重要的写在前面，排序会把它埋到中间。

        这里**不脱敏**：文件在用户自己的磁盘上，脱敏发生在展示与导出边界
        （`read_task_log` / `diagnostics_text`）。在写入侧脱敏等于把本机排查
        也一起弄瞎，而这恰恰是唯一能看到完整鉴权 URL 的地方。
        """
        try:
            parts = [f'EVENT {kind}']
            for key, value in fields.items():
                parts.append(f'{key}={_fmt_field(value)}')
            self._logger.info(' '.join(parts))
        except Exception:
            pass

    # -- 生命周期 -----------------------------------------------------------

    def close(self) -> None:
        """摘掉并关闭文件 handler。幂等，不抛。

        必须摘掉而不是只 close：logger 对象是进程级缓存的，留着一个已关闭的
        handler 在上面，同 id 的任务重跑（用户点「重试」）时会往一个关掉的
        流里 emit，每条记录走一次 handleError。
        """
        handler, self._handler = self._handler, None
        if handler is None:
            return
        try:
            self._logger.removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 有异常时先把它记进本任务日志再关闭 —— 否则任务失败的**原因**只在
        # 上层的 catch-all 里，而用户点开的是这个任务的日志。
        if exc_type is not None:
            self.exception('任务执行中断：%s: %s', exc_type.__name__, exc)
        self.close()
        return False  # 绝不吞任务本身的异常


def _fmt_field(value) -> str:
    """事件字段值的书写形式：需要引号时才加引号。"""
    try:
        text = '' if value is None else str(value)
    except Exception:
        return '<unprintable>'
    if text == '' or '=' in text or any(c.isspace() for c in text):
        return repr(text)
    return text


def open_task_log(pipeline: str, task_id: int, config_manager=None) -> TaskLogger:
    """打开一个任务的日志句柄。**永不返回 None，也不因环境问题抛**。

    `task_log_enabled=false` 时返回的 TaskLogger 没有文件 handler，消息照旧
    传播到全局日志。返回 None 会让每个调用点都长出一个 `if tlog:` ——
    四条管线乘上每个阶段，漏一处就是一次 AttributeError 把任务打成 failed。

    尺寸上限取 `task_log_max_kb`（RotatingFileHandler + backupCount=1：留一份
    备份，这样刚轮转完也还能看到之前那段，见 read_task_log 的补读）。

    不 mkdir、不建文件：`delay=True` + `_TaskFileHandler._open()` 里建目录，
    所以只开不写的进程在磁盘上不留痕迹。

    Raises:
        ValueError: pipeline / task_id 非法（见 task_log_path）。**只有这一条**
            会抛：参数是脏的意味着调用方拼错了标识，静默写到别的文件里比报错
            糟得多。环境问题（下面那个 except）一律降级。
    """
    try:
        path = task_log_path(pipeline, task_id)
    except ValueError:
        raise
    except Exception as e:
        # 环境问题，不是调用方的错：`Config.BASE_DIR` 被配成 str 而不是 Path 时
        # `/` 抛的是 TypeError，打包 / 嵌入场景下 BASE_DIR 也可能整个拿不到。
        # 这里必须降级而不是抛，因为调用点是四条管线线程体的**第一条语句**，
        # 在写终态的 try/finally 之前 —— 抛出会让线程直接死掉，任务行永远卡在
        # running，连滞留任务补偿都不会触发。一个次要 sink 的环境问题不该有把
        # 任务打死的权力（同 TaskLogger 的类 docstring）。
        _warn_once('path', '每任务日志目录不可用（%s #%s）：%s。本次运行只写全局日志。',
                   pipeline, task_id, e)
        # 走到这里说明 pipeline 与 task_id 都已经过了 task_log_path 的校验
        # （它先校验、再拼路径），所以 int() 是安全的。
        return TaskLogger(pipeline, int(task_id))
    tid = int(task_id)
    tlog = TaskLogger(pipeline, tid)
    if not _bool_config('task_log_enabled', config_manager):
        return tlog

    max_kb = _int_config('task_log_max_kb', config_manager)
    try:
        handler = _TaskFileHandler(
            str(path),
            maxBytes=max(1, max_kb) * 1024,
            # 只留一份备份：这是「解释一个任务」用的日志，不是审计留存。
            # 两份 4 MB 的上限乘上历史任务数已经是治理问题（见 prune_task_logs）。
            backupCount=1,
            # encoding 必填：不给的话 Windows 用 cp936 写中文日志，遇到
            # 生僻字/emoji 直接在 emit 里 UnicodeEncodeError（同 logging_setup:149）。
            encoding='utf-8',
            # 到第一条记录才建文件与目录。
            delay=True)
    except Exception as e:
        # 构造 RotatingFileHandler 在 delay=True 下几乎不碰盘，但路径畸形
        # （超长、非法字符）仍会在这里炸。日志是次要功能，不该让任务起不来。
        _warn_once(f'open:{path}', '每任务日志无法启用（%s）：%s。本任务只写全局日志。',
                   path, e)
        return tlog
    handler.setFormatter(_TaskFileFormatter())
    # 同一个 (pipeline, task_id) 重开（重试、恢复）会拿到**同一个** logger
    # 对象。旧 handler 不摘掉就是两份 handler 两倍行数，而且旧那个还指向
    # 已经轮转走的 inode。
    _detach_task_handlers(tlog._logger)
    tlog._logger.addHandler(handler)
    tlog._handler = handler
    return tlog


def _detach_task_handlers(target: logging.Logger) -> None:
    """摘掉该 logger 上遗留的每任务 handler（只认自己这个类，不碰别人的）。"""
    for existing in list(getattr(target, 'handlers', ())):
        if not isinstance(existing, _TaskFileHandler):
            continue
        try:
            target.removeHandler(existing)
            existing.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _tail_lines(path: Path, limit: int) -> list[str]:
    """反向按块读文件尾部，最多返回 limit 行完整的行。

    不 readlines()：上限是 4 MB（task_log_max_kb 最大可配到 1 GB），而 UI 每次
    只要 500 行。反向读必然从块中间切进一行，所以除了确实读到文件头的情形，
    第一行一律丢掉 —— 留着它就是一条被砍头的、时间戳解析不出来的假记录。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    want = max(1, int(limit))
    chunks: list[bytes] = []
    newlines = 0
    pos = size
    try:
        with open(path, 'rb') as f:
            # 多读一个换行：要 want 行完整的行就得看到 want+1 个换行边界。
            while pos > 0 and newlines <= want:
                step = min(_TAIL_CHUNK, pos)
                pos -= step
                f.seek(pos)
                block = f.read(step)
                if not block:
                    break
                chunks.append(block)
                newlines += block.count(b'\n')
    except OSError:
        return []
    chunks.reverse()
    # errors='replace' 而不是 'strict'：轮转边界或崩溃时文件尾可能是半个
    # UTF-8 序列，为一个坏字节丢掉整段日志是最差的取舍。
    lines = b''.join(chunks).decode('utf-8', errors='replace').splitlines()
    if pos > 0 and lines:
        lines = lines[1:]
    return lines[-want:]


def read_task_log(pipeline, task_id, *, limit=500, errors_only=False) -> list[dict]:
    """读一个任务日志的尾部，解析成 `{'ts','level','message'}`，最旧的在前。

    文件不存在（任务没跑过、日志被关掉、已被 prune）返回 `[]` —— 这不是错误，
    UI 上就该是「暂无日志」。任何读取异常也返回已经拿到的部分，绝不抛：
    这个函数挂在一条 HTTP 路由后面，为一个 IO 抖动回 500 没有意义。

    当前文件不够 limit 行时补读一次轮转备份（`.log.1`）。不补的话「刚好轮转
    完」这一瞬间用户看到的是一个几乎空白的日志，而任务已经跑了两小时 ——
    这恰恰是最容易让人以为「日志坏了」的时刻。

    message 过 `redact`：这个返回值直接进 HTTP 响应。
    """
    try:
        path = task_log_path(pipeline, task_id)
    except ValueError:
        # 读侧对脏参数不抛：拼错的标识等价于「没有这个任务的日志」。
        return []
    want = max(1, int(limit or 1))
    lines = _tail_lines(path, want)
    if len(lines) < want:
        backup = Path(f'{path}.1')
        head_room = want - len(lines)
        lines = _tail_lines(backup, head_room) + lines
    entries = _parse_entries(lines)
    if errors_only:
        entries = [e for e in entries if e['level'] in _ERROR_LEVELS]
    return entries[-want:]


def _parse_entries(lines) -> list[dict]:
    """把日志行解析成条目；多行回溯的续行并入上一条的 message。

    续行单独成条会让「只看错误」把回溯的每一行都当成 level 未知的记录丢掉 ——
    而回溯正是错误里信息量最大的部分。
    """
    entries: list[dict] = []
    for line in lines:
        m = _ENTRY_RE.match(line)
        if m:
            entries.append({
                'ts': m.group(1),
                'level': m.group(2),
                'message': redact(m.group(3)),
            })
        elif entries:
            entries[-1]['message'] += '\n' + redact(line)
        else:
            # 尾部截断正好切在回溯中间：留着内容但不假装知道级别/时间。
            entries.append({'ts': '', 'level': '', 'message': redact(line)})
    return entries


def diagnostics_text(pipeline, task_id, *, limit=2000) -> str:
    """脱敏的纯文本诊断包 —— §4.5 的「复制诊断摘要 / 导出脱敏诊断包」。

    头部先给环境（版本 / 平台 / Python / 生成时间 / 任务标识），再接日志尾部。
    环境放最前面是因为这段文本的用途是**发给别人看**：拿到一段日志却不知道
    是哪个版本哪个平台，第一个来回一定是在问这个。

    全程不抛：导出诊断包的按钮在「已经出问题了」的时候按，它自己再报一个错
    是最糟的体验。任何环节失败就在正文里写明哪一段拿不到。
    """
    lines = ['=== TerraForge 诊断摘要 ===']
    try:
        from src.core.config import Config
        version = getattr(Config, 'APP_VERSION', '?')
    except Exception:
        version = '?'
    lines.append(f'版本      : {version}')
    try:
        lines.append(f'平台      : {platform.platform()}')
        lines.append(f'Python    : {platform.python_version()} ({sys.platform})')
    except Exception:
        lines.append('平台      : <获取失败>')
    try:
        from src.core.database import utc_now_iso
        lines.append(f'生成时间  : {utc_now_iso()}')
    except Exception:
        lines.append('生成时间  : <获取失败>')
    lines.append(f'任务      : {pipeline} #{task_id}')
    try:
        lines.append(f'日志文件  : {redact(str(task_log_path(pipeline, task_id)))}')
    except ValueError as e:
        lines.append(f'日志文件  : <参数非法：{e}>')
    lines.append('')
    lines.append(f'--- 日志尾部（最多 {int(limit)} 行，已脱敏）---')
    try:
        entries = read_task_log(pipeline, task_id, limit=limit)
    except Exception as e:
        entries = []
        lines.append(f'<日志读取失败：{e}>')
    if not entries:
        lines.append('(无日志记录)')
    for entry in entries:
        prefix = ' '.join(x for x in (entry['ts'], entry['level']) if x)
        lines.append(f'{prefix} {entry["message"]}'.strip())
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# 保留期治理
# ---------------------------------------------------------------------------

def prune_task_logs(config_manager=None) -> int:
    """删掉超过 `task_log_retain_days` 的任务日志，返回删除的文件数。不抛。

    按 mtime 判定而不是按任务是否还在跑：保留期最小 1 天（见 config_manager 的
    区间），而在跑的任务每写一行就刷新 mtime，所以活着的任务天然不会被删。
    按任务状态判定反而要读库，把一个纯文件清理变成一个会被 database is locked
    卡住的操作。

    只删名字符合 `_LOG_NAME_RE` 的文件（含 `.log.1` 备份）。目录里别的东西
    一概不碰：全局的 terraforge.log 不在这个目录，但用户往 logs/tasks 里放过
    东西这种事是会发生的，而一个「清理日志」的功能删掉用户的文件不可接受。
    """
    days = _int_config('task_log_retain_days', config_manager)
    if days <= 0:
        # 非法值已经在 _int_config 里退回过出厂值；真是 0 就当不清理，
        # 而不是把 cutoff 算成「现在」把所有日志删光。
        return 0
    cutoff = time.time() - days * 86400.0
    directory = task_log_dir()
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        # 目录还没建（从没跑过任务）是最常见的情形，不值得一条警告。
        return 0
    for name in names:
        if not _LOG_NAME_RE.match(name):
            continue
        target = directory / name
        try:
            if target.stat().st_mtime >= cutoff:
                continue
            target.unlink()
            removed += 1
        except OSError as e:
            # Windows 上正在被 handler 持有的文件删不掉，下一轮再说。
            logger.debug('清理任务日志 %s 失败（忽略）：%s', target, e)
    if removed:
        logger.info('清理了 %d 个超过 %d 天的任务日志', removed, days)
    return removed
