"""task_logging —— 每任务日志的落盘时机、脱敏与「绝不抛」。

三条互相独立的契约在这里立成回归：

1. **只开不写的进程在磁盘上不留痕迹。** 目录在第一条记录真的落盘时才建；
   否则 `import` 或一次 open 就会在仓库里长出 `logs/`（
   `tests/test_no_repo_pollution.py` 明令禁止），CI 的干净 clone 上直接红。
2. **展示与导出边界必须脱敏。** `read_task_log` 的返回值直接进 HTTP 响应，
   而任务日志里恰恰是唯一能看到完整鉴权 URL 的地方。
3. **整个接口面不抛。** 这些方法会被塞进 GDAL 的进度回调；回调抛出会被 GDAL
   当成用户主动中止，于是它**删掉已经写了一大半的输出文件**。一次 ENOSPC 或
   Windows 上的文件锁不该有作废一个 99% 完成的产物的权力。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeConfig:
    """只回答 `get` 的配置替身。ConfigManager 会把 database 整条链拉起来。"""

    def __init__(self, **values):
        self.values = {str(k): str(v) for k, v in values.items()}

    def get(self, key, default=None):
        return self.values.get(key, default)


ENABLED = FakeConfig(task_log_enabled='true', task_log_max_kb='64',
                     task_log_retain_days='14')
DISABLED = FakeConfig(task_log_enabled='false', task_log_max_kb='64',
                      task_log_retain_days='14')


# ---------------------------------------------------------------------------
# 落盘时机
# ---------------------------------------------------------------------------

def test_no_file_and_no_directory_until_the_first_record():
    """open_task_log 不许碰文件系统 —— 仓库污染那条红线就在这里。"""
    from src.services.task_logging import open_task_log, task_log_dir

    tlog = open_task_log('map', 1, ENABLED)
    try:
        assert tlog.enabled is True
        assert tlog.path is not None
        assert not tlog.path.exists()
        assert not task_log_dir().exists()

        tlog.info('第一条记录')
        assert tlog.path.exists()
        assert task_log_dir().is_dir()
    finally:
        tlog.close()


def test_disabled_logging_writes_nothing_and_still_accepts_every_call():
    """关掉之后返回的仍是一个可用句柄 —— 返回 None 会让每个调用点长出 `if tlog:`。"""
    from src.services.task_logging import open_task_log, task_log_dir

    tlog = open_task_log('map', 2, DISABLED)
    try:
        assert tlog.enabled is False
        assert tlog.path is None
        tlog.info('x')
        tlog.warning('y')
        tlog.event('state_change', to='failed')
        assert not task_log_dir().exists()
    finally:
        tlog.close()


def test_close_is_idempotent():
    from src.services.task_logging import open_task_log

    tlog = open_task_log('map', 3, ENABLED)
    tlog.info('x')
    tlog.close()
    tlog.close()
    assert tlog.path is None


def test_reopening_the_same_task_does_not_double_the_lines():
    """logger 对象是进程级缓存的：旧 handler 不摘掉就是两份 handler 两倍行数。"""
    from src.services.task_logging import open_task_log, read_task_log

    first = open_task_log('map', 4, ENABLED)
    first.info('唯一一行')
    second = open_task_log('map', 4, ENABLED)   # 用户点了「重试」
    try:
        second.info('第二行')
    finally:
        second.close()
        first.close()
    messages = [e['message'] for e in read_task_log('map', 4)]
    assert messages.count('第二行') == 1


@pytest.mark.parametrize('pipeline', ['../../data/map_downloader', 'nope', ''])
def test_a_bad_pipeline_identifier_is_refused(pipeline):
    """pipeline 与 task_id **直接变成文件名**，都从 HTTP 请求来。

    不校验的话一个 `'../../data/map_downloader'` 就能让日志 handler 去覆盖
    数据库文件。
    """
    from src.services.task_logging import open_task_log, task_log_path

    with pytest.raises(ValueError):
        task_log_path(pipeline, 1)
    with pytest.raises(ValueError):
        open_task_log(pipeline, 1, ENABLED)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

def test_redact_removes_a_bearer_token():
    from src.services.task_logging import redact

    out = redact("请求头 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def 已发出")
    assert 'eyJhbGciOiJIUzI1NiJ9' not in out
    assert 'abc.def' not in out


def test_redact_removes_a_query_string_key():
    from src.services.task_logging import redact

    out = redact('fetch https://tiles.example/x?key=SECRET-VALUE-123&z=5 -> 200')
    assert 'SECRET-VALUE-123' not in out
    assert '200' in out, '值的终止符没圈住，把后面的有用信息一起吞了'


def test_redact_masks_url_userinfo_but_keeps_the_host():
    """掩成 `***:***@host`：排查时要看得见主机，但不能把凭据写进响应。"""
    from src.services.task_logging import redact

    out = redact('fetch failed: https://alice:hunter2@nominatim.example/search -> 500')
    assert 'hunter2' not in out
    assert 'alice' not in out
    assert 'nominatim.example' in out


@pytest.mark.parametrize('text, secret', [
    ('access_key=AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
    ("{'token': 'abc123xyz'}", 'abc123xyz'),
    ('earthdata_password=p@ssw0rd', 'p@ssw0rd'),
    ('Cookie: session=deadbeef', 'deadbeef'),
    ('X-Api-Key: 9f8e7d6c', '9f8e7d6c'),
    ('signature=abcdef0123', 'abcdef0123'),
])
def test_redact_covers_the_common_credential_spellings(text, secret):
    """下划线是 word 字符 —— `\\bkey` 这种写法会**漏掉** `access_key=`。

    `{'token': 'abc'}` 这条走的是 dict 字面量的冒号形式：`key_eq_value` 只认
    等号，而 `header_colon_value` 只列了具体的头名字，两条都盖不住它。少盖一次
    就是一次凭据泄漏（诊断包是要发给别人看的），所以它必须在清单里。
    """
    from src.services.task_logging import redact

    assert secret not in redact(text)


def test_redact_is_total_and_never_raises():
    """脱敏失败时绝不 fallback 回原文 —— 原文正是那个带密码的字符串。"""
    from src.services.task_logging import redact

    for weird in (None, 123, b'bytes', object(), '\x00\x01', 'x' * 5000):
        assert isinstance(redact(weird), str)


def test_read_task_log_redacts_what_the_file_holds_in_the_clear():
    """写入侧**不**脱敏（本机排查需要完整 URL），展示侧必须脱敏。"""
    from src.services.task_logging import open_task_log, read_task_log

    tlog = open_task_log('dem', 11, ENABLED)
    try:
        tlog.info('GET https://user:pw@dem.example/g.tif?token=TOP-SECRET')
        raw = tlog.path.read_text(encoding='utf-8')
    finally:
        tlog.close()
    assert 'TOP-SECRET' in raw            # 文件在用户自己的盘上，留着能查

    entries = read_task_log('dem', 11)
    assert entries and all('TOP-SECRET' not in e['message'] for e in entries)
    assert all('pw@' not in e['message'] for e in entries)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def test_read_task_log_of_a_task_that_never_ran_is_empty():
    """不是错误，UI 上就该是「暂无日志」。"""
    from src.services.task_logging import read_task_log

    assert read_task_log('map', 999999) == []


def test_read_task_log_of_a_bad_identifier_is_empty_not_an_exception():
    """读侧对脏参数不抛：拼错的标识等价于「没有这个任务的日志」。"""
    from src.services.task_logging import read_task_log

    assert read_task_log('../etc', 1) == []
    assert read_task_log('map', 'abc') == []


def test_read_task_log_parses_level_and_keeps_the_oldest_first():
    from src.services.task_logging import open_task_log, read_task_log

    tlog = open_task_log('contour', 21, ENABLED)
    try:
        tlog.info('第一步')
        tlog.warning('重试一次')
        tlog.error('失败了')
    finally:
        tlog.close()

    entries = read_task_log('contour', 21)
    assert [e['message'] for e in entries] == ['第一步', '重试一次', '失败了']
    assert [e['level'] for e in entries] == ['INFO', 'WARNING', 'ERROR']
    assert all(e['ts'] for e in entries)


def test_errors_only_keeps_warnings():
    """重试、429、无覆盖这些**恰好**是 WARNING，而它们正是「为什么只下到一半」
    的答案。只留 ERROR 会让这个开关在最常见的排查场景下给出一片空白。"""
    from src.services.task_logging import open_task_log, read_task_log

    tlog = open_task_log('contour', 22, ENABLED)
    try:
        tlog.info('普通进度')
        tlog.warning('429，退避重试')
        tlog.error('放弃')
    finally:
        tlog.close()

    levels = [e['level'] for e in read_task_log('contour', 22, errors_only=True)]
    assert levels == ['WARNING', 'ERROR']


def test_structured_events_are_one_parseable_line():
    """状态机每次转换都走 event()。带空白的值要加引号，否则字段会被拆成两个。"""
    from src.services.task_logging import open_task_log, read_task_log

    tlog = open_task_log('map', 31, ENABLED)
    try:
        tlog.event('state_change', to='failed', reason='disk full')
    finally:
        tlog.close()

    message = read_task_log('map', 31)[-1]['message']
    assert message.startswith('EVENT state_change')
    assert 'to=failed' in message
    assert "reason='disk full'" in message


def test_diagnostics_text_is_a_redacted_string():
    from src.services.task_logging import diagnostics_text, open_task_log

    tlog = open_task_log('map', 32, ENABLED)
    try:
        tlog.info('GET https://u:p@x.example/a?key=NOPE')
    finally:
        tlog.close()
    text = diagnostics_text('map', 32)
    assert isinstance(text, str) and text
    assert 'NOPE' not in text


# ---------------------------------------------------------------------------
# 绝不抛
# ---------------------------------------------------------------------------

@pytest.fixture
def unwritable_base(monkeypatch, tmp_path):
    """把 BASE_DIR 指到一个**文件**上：任何 mkdir 都会失败。

    比 chmod 可靠 —— root 用户和某些文件系统会让只读位形同虚设，而
    「父路径是文件」在所有平台上都必然失败。
    """
    from src.core import config

    blocker = tmp_path / 'i-am-a-file'
    blocker.write_text('not a directory')
    monkeypatch.setattr(config.Config, 'BASE_DIR', blocker, raising=False)
    return blocker


def test_the_whole_surface_survives_an_unwritable_log_directory(unwritable_base):
    """日志是次要 sink，它的环境问题不该有把任务打死的权力。

    open_task_log 是四条管线线程体的**第一条语句**，在写终态的 try/finally
    之前 —— 抛出会让线程直接死掉，任务行永远卡在 running。
    """
    from src.services.task_logging import (
        diagnostics_text, open_task_log, prune_task_logs, read_task_log,
    )

    tlog = open_task_log('map', 41, ENABLED)
    try:
        tlog.debug('d')
        tlog.info('i')
        tlog.warning('w')
        tlog.error('e')
        tlog.event('state_change', to='failed')
        try:
            raise RuntimeError('boom')
        except RuntimeError:
            tlog.exception('炸了')
    finally:
        tlog.close()

    assert read_task_log('map', 41) == []
    assert isinstance(diagnostics_text('map', 41), str)
    assert prune_task_logs(ENABLED) == 0


def test_the_context_manager_never_swallows_the_task_exception():
    """`__exit__` 把异常记进本任务日志之后必须放行 —— 吞掉就是任务静默「成功」。"""
    from src.services.task_logging import open_task_log, read_task_log

    with pytest.raises(RuntimeError, match='真实失败'):
        with open_task_log('local_terrain', 51, ENABLED) as tlog:
            tlog.info('开始')
            raise RuntimeError('真实失败')

    messages = '\n'.join(e['message'] for e in read_task_log('local_terrain', 51))
    assert '真实失败' in messages


def test_logging_a_broken_format_string_does_not_raise():
    """Formatter 遇到坏的 %s 参数会炸 —— 那不该传染给任务本身。"""
    from src.services.task_logging import open_task_log

    tlog = open_task_log('map', 61, ENABLED)
    try:
        tlog.info('%d %d', 'not-a-number')
        tlog.event('weird', value=object())
    finally:
        tlog.close()
