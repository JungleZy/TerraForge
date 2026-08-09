"""路径类与 URL 类配置键的校验（2026-08-08 评审「安全姿态」第 3 项）。

**旧行为**：`config_manager.validate_config` 是一串 if/elif，只覆盖 10 个键，
结尾一句 `# For keys without specific validation, accept any value; return True`
把**全部**路径类与 URL 类键放行。评审文档 `:209-212` 实测跑通了这条链：

    PUT /api/config {"terrain_global_base_path":"/"}  -> 200 {"updated":[...]}
    GET /terrain/base/etc/passwd                      -> 200, 1427 bytes

因为 `/terrain/base/<path>` 的根就是这个配置键，根一旦是 `/`，
`terrain_static._resolve_safe_file` 的 `relative_to` 包含检查恒真。
同类的 `stitch_tmpdir` 会被 `download_engine.py` 直接
`os.makedirs(..., exist_ok=True)`；`terrain_base_parent_url` 被固化进
`layer.json` 的 `parentUrl` 交给浏览器。

**为什么这里的断言不是空断言**：`validate_config` 对未登记的键返回 True，
所以「随便挑一个值断言 True」在旧代码上照样绿。每一条拒绝用例挑的都是旧代码
**必然返回 True** 的值（旧代码对这四个键从不返回 False），因此把实现换回
`return True` 兜底，本文件的所有 `is False` 断言都会变红。反向的接受用例则守住
出厂默认值与文档化的部署形态，防止「一刀切拒绝」这种假修复。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.config import Config                      # noqa: E402
from src.services import config_manager as cm_mod       # noqa: E402
from src.services.config_manager import ConfigManager   # noqa: E402


def _defaults():
    """DEFAULT_CONFIGS 只在用例内部惰性取。

    src.core.database 被约 40 个测试文件裸 `sys.modules.pop`，任何模块级
    `from src.core.database import ...` 都会造成「一个测试持有旧模块对象、
    另一个重导入出新的」这种双实例，tests/test_conftest_isolation_contract.py
    的棘轮用例专门盯着这条（本文件曾经踩中）。
    """
    from src.core.database import DEFAULT_CONFIGS

    return dict(DEFAULT_CONFIGS)


@pytest.fixture
def cm(isolated_app):
    """isolated_app 把 DATABASE_PATH / DOWNLOADS_DIR / CACHE_DIR 指到 tmp_path
    并建好库；BASE_DIR 保持仓库根（随包 assets/ 就在那儿）。"""
    return ConfigManager()


# --------------------------------------------------------------------------
# 路径类：terrain_global_base_path
# --------------------------------------------------------------------------

# 2026-08-08：用户裁定「系统运行在可信环境中，不用考虑安全」，随之取消了路径类
# 键的**根目录约束**。下面三条原来钉的是「落在 BASE_DIR/DOWNLOADS_DIR/CACHE_DIR
# 之外一律拒」，那是安全边界；现在钉的是**功能性**判据。改判的理由写在
# config_manager._validate_base_terrain_path / _validate_scratch_dir 里。

def test_base_path_on_another_disk_is_accepted(cm):
    """随包底图 224 MB / 4.3 万个文件，放到另一块盘是正常需求。

    这条原来叫 test_base_path_absolute_outside_roots_is_rejected，断言相反 ——
    根目录约束取消后它就是在拦一个正当用法。
    """
    outside = str(Path(tempfile.gettempdir()) / 'terraforge_base_on_other_disk')
    assert cm.validate_config('terrain_global_base_path', outside) is True


def test_base_path_relative_is_accepted(cm):
    """出厂值 `./assets/terrain/base_z8` 本身就是相对的，不能拒相对值。"""
    assert cm.validate_config(
        'terrain_global_base_path', './assets/terrain/base_z8') is True
    assert cm.validate_config('terrain_global_base_path', '../sibling/base') is True


def test_base_path_empty_is_rejected(cm):
    """留空会让 /terrain/base 的根落到 BASE_DIR 本身 —— 等于把整个安装目录
    （含 data/map_downloader.db）挂上静态服务。"""
    assert cm.validate_config('terrain_global_base_path', '') is False


def test_shipped_default_base_path_is_still_accepted(cm):
    """出厂默认值必须活着：它是相对路径，一刀切「只收绝对路径」就会把
    随包底图整条链打断（底图判不可用 → parentUrl 兜底 → heightmap 陷阱）。"""
    defaults = _defaults()
    assert cm.validate_config(
        'terrain_global_base_path', defaults['terrain_global_base_path']) is True
    # 值本身也钉一下，免得默认值漂了而用例还绿
    assert defaults['terrain_global_base_path'] == './assets/terrain/base_z8'


def test_base_path_absolute_inside_downloads_dir_is_accepted(cm):
    """自建的全球 base 放在 downloads/ 下是正常用法。"""
    inside = str(Path(Config.DOWNLOADS_DIR) / 'terrain' / 'base')
    assert cm.validate_config('terrain_global_base_path', inside) is True


def test_base_path_downloads_prefixed_relative_is_accepted(cm):
    """`./downloads/...` 走 resolve_stored_output_dir 的前缀剥离口径，
    落到 DOWNLOADS_DIR 之内 —— 与读取侧同一套解析。"""
    assert cm.validate_config(
        'terrain_global_base_path', './downloads/terrain/base') is True


# --------------------------------------------------------------------------
# 路径类：两个 tmpdir 键
# --------------------------------------------------------------------------

@pytest.mark.parametrize('key', ['stitch_tmpdir', 'contour_warp_tmpdir'])
def test_tmpdir_empty_means_system_temp_and_is_accepted(cm, key):
    """出厂值就是空串（= 用系统临时目录），拒绝它等于开箱即坏。"""
    assert _defaults()[key] == ''
    assert cm.validate_config(key, '') is True


@pytest.mark.parametrize('key', ['stitch_tmpdir', 'contour_warp_tmpdir'])
def test_tmpdir_system_temp_is_accepted(cm, key):
    """这两个键存在的意义就是把 GB 级中间产物挪到别的盘，temp 根必须允许。"""
    assert cm.validate_config(key, tempfile.gettempdir()) is True
    assert cm.validate_config(
        key, str(Path(tempfile.gettempdir()) / 'terraforge_scratch')) is True


@pytest.mark.parametrize('key', ['stitch_tmpdir', 'contour_warp_tmpdir'])
def test_tmpdir_on_any_absolute_path_is_accepted(cm, key):
    """把 scratch 放到任意一块盘 —— 这两个键存在的**全部**意义。

    原来这条叫 test_tmpdir_outside_every_root_is_rejected，断言 `/etc/terraforge`
    与 `/` 一律拒。那条根目录约束与这两个键的用途直接冲突（上一条用例的
    docstring 自己写着「挪到别的盘」，而规则不许挪出安装目录），可信环境前提
    确认后取消。
    """
    # 「绝对」在两个平台上不是一回事：ntpath 下 `/mnt/...` 与 `/` 都是**根相对**
    # 路径（挂在当前盘上），Path.is_absolute() 判 False。这正是校验器应当拒的那
    # 类不确定路径（与下一条用例拒相对值同一个理由），所以要钉「另一块盘照收」
    # 就得各平台各写各的 —— 硬编码 POSIX 写法会让 Windows 发版构建变红。
    other_disk = (r'D:\fast-ssd\terraforge-scratch' if os.name == 'nt'
                  else '/mnt/fast-ssd/terraforge-scratch')
    fs_root = 'C:\\' if os.name == 'nt' else '/'
    assert cm.validate_config(key, other_disk) is True
    assert cm.validate_config(key, fs_root) is True


@pytest.mark.parametrize('key', ['stitch_tmpdir', 'contour_warp_tmpdir'])
def test_tmpdir_relative_is_rejected(cm, key):
    """相对值仍然拒 —— 这条与安全无关，是正确性。

    `download_engine` 那侧是 `os.makedirs(stitch_tmp_base)`，相对值按【进程
    CWD】解析；打包 exe 从快捷方式启动时 CWD 不是安装目录，GB 级中间产物会落到
    一个谁也想不到的位置。同一类坑 M10 已经给 output_path 修过一遍。
    """
    assert cm.validate_config(key, 'scratch') is False
    assert cm.validate_config(key, './tmp/warp') is False


# --------------------------------------------------------------------------
# URL 类：terrain_base_parent_url
# --------------------------------------------------------------------------

def test_parent_url_link_local_metadata_is_rejected(cm):
    """169.254.169.254 是云厂商实例元数据端点，不可能是地形服务地址。"""
    assert cm.validate_config(
        'terrain_base_parent_url', 'http://169.254.169.254/x') is False


def test_parent_url_same_origin_relative_is_accepted(cm):
    """同源相对路径是最稳的写法（换端口/反代都不用改）。"""
    assert cm.validate_config('terrain_base_parent_url', '/terrain/base') is True


@pytest.mark.parametrize('value', [
    'javascript:alert(1)',
    'data:application/json,{}',
    'file:///etc/passwd',
    '//evil.example.com/terrain/base',      # 协议相对：绕开 scheme 白名单
    'http://user:pw@tiles.example.com/base',  # userinfo 会随 layer.json 发到浏览器
    'http://tiles.example.com:99999/base',    # 端口越界
    'http://tiles.example.com/ba se',         # 空白字符
])
def test_parent_url_rejects_values_that_should_never_reach_a_browser(cm, value):
    assert cm.validate_config('terrain_base_parent_url', value) is False


@pytest.mark.parametrize('value', [
    'http://localhost:5000/terrain/base',           # 出厂默认值
    'http://192.168.1.10:5000/terrain/base',        # 文档化的内网部署
    'https://tiles.example.com:8443/terrain/base',  # test_local_terrain_api 用的值
    'http://terrain.internal:9000/terrain/base/layer.json',
    '',                                             # 空 = 不写 parentUrl
])
def test_parent_url_keeps_the_documented_deployment_shapes(cm, value):
    """**有意偏离评审建议的一条**：不拦回环/私网。

    出厂默认值就是 `http://localhost:5000/terrain/base`，而
    `docs/reference/terrain/global-base-build.md:131` 明写改这个键的典型场景
    就是「换端口、部署到内网 IP 或域名」。按「非回环非私网」一刀切会把默认值
    和文档化的部署方式一起判非法，而对真正的威胁（指到攻击者的**公网**站点）
    毫无作用。拦的是链路本地段与非 http(s)/带 userinfo 的形态。
    """
    assert cm.validate_config('terrain_base_parent_url', value) is True


def test_parent_url_default_survives_validation(cm):
    assert _defaults()['terrain_base_parent_url'] == 'http://localhost:5000/terrain/base'


# --------------------------------------------------------------------------
# URL 类：proxy_url
# --------------------------------------------------------------------------

@pytest.mark.parametrize('value,ok', [
    ('', True),                                          # 空 = 自动探测/直连
    ('http://proxy.example.com:8080', True),             # 输入框 placeholder 的形态
    ('http://proxyuser:p%40ssw0rd@127.0.0.1:7890', True),  # 认证代理，回环不拦
    ('https://proxy.corp.example', True),                # 省略端口
    ('127.0.0.1:7890', False),                           # 缺 scheme
    ('socks5://127.0.0.1:1080', False),                  # aiohttp 不支持
    ('http://', False),                                  # 缺主机
])
def test_proxy_url_shape(cm, value, ok):
    """manual proxy_url 被 resolve_proxy_url 原样交给 aiohttp（不做归一），
    缺 scheme 的值存进去之后每一张瓦片都在 aiohttp 里抛 InvalidURL —— 报错
    离配置页十万八千里。判据与 proxy_autodetect._normalize_proxy_url 一致。"""
    assert cm.validate_config('proxy_url', value) is ok


# --------------------------------------------------------------------------
# 枚举类：terrain_quality_preset
# --------------------------------------------------------------------------

@pytest.mark.parametrize('value,ok', [
    ('precision', True),
    ('balanced', True),
    ('speed', True),
    ('fast', False),        # 不存在的档位
    ('', False),            # 空值不等于“用默认”
    ('Balanced', False),    # 刻意不做大小写归一
])
def test_tiling_quality_shape(cm, value, ok):
    """档位是枚举，脏值必须被配置接口拒掉。

    白名单从 geo_validation.TILING_QUALITY_OFFSETS 取，不在 config_manager
    里抄第二份 —— 见 _UNCONSTRAINED_KEYS 注释里关于“第二处事实来源”的说明。
    """
    assert cm.validate_config('terrain_quality_preset', value) is ok


# --------------------------------------------------------------------------
# 治理：不允许再有键靠「什么都不写」拿到 accept-anything
# --------------------------------------------------------------------------

def test_every_default_config_key_is_registered():
    """新增配置键必须在 _VALUE_RULES 或 _UNCONSTRAINED_KEYS 里显式登记。

    这条用例才是「改成表驱动」的实际收益：旧写法下新键默认落进
    `return True`，没有任何东西会提醒作者。
    """
    declared = set(_defaults())
    registered = set(cm_mod._VALUE_RULES) | cm_mod._UNCONSTRAINED_KEYS
    assert declared - registered == set(), '新配置键未登记校验规则'
    assert registered - declared == set(), '规则表里有 DEFAULT_CONFIGS 之外的键'
    assert not (set(cm_mod._VALUE_RULES) & cm_mod._UNCONSTRAINED_KEYS)


def test_all_shipped_defaults_pass_their_own_rule(cm):
    """出厂默认值必须自洽 —— reset_to_defaults 绕过校验直接重插，
    默认值若过不了自己的规则，之后随便存一次配置就整批 400。
    （default_save_path 是已知例外：'./downloads' 由 normalize_default_save_path
    在重插后立刻归一成绝对路径，见 database.py 的注释。）"""
    for key, value in _defaults().items():
        if key == 'default_save_path':
            continue
        assert cm.validate_config(key, value) is True, f'默认值过不了自己的规则: {key}'


# --------------------------------------------------------------------------
# 端到端：PUT 的校验闸门还在，只是判据换了
# --------------------------------------------------------------------------

def test_put_config_rejects_an_unusable_base_path(isolated_app, tmp_path):
    """空值仍然 400，且配置行原样不动。

    这条原来叫 test_put_config_rejects_the_arbitrary_file_read_root，喂的是
    `/`（评审实测那条任意文件读的第一步）。可信环境前提确认后 `/` 不再是安全
    问题，根目录约束取消；留下来的判据是**功能性**的：空值会让 /terrain/base
    的根落到 BASE_DIR 本身，把整个安装目录（含 data/map_downloader.db）挂上
    静态服务，而且底图判定必然失败。
    """
    client = isolated_app.app.test_client()

    resp = client.put('/api/config', json={'terrain_global_base_path': ''})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body['success'] is False
    assert body['updated'] == []
    assert any('terrain_global_base_path' in e for e in body['errors'])
    assert ConfigManager().get('terrain_global_base_path') == './assets/terrain/base_z8'


def test_put_config_accepts_a_base_path_on_another_disk(isolated_app, tmp_path):
    """反面：另一块盘上的绝对路径必须收下并真的落库。

    根目录约束取消之后，这才是这个键的主要用法（224 MB 的底图放大盘）。
    没有这条，上面那个 400 用例可能是在一刀切拒绝。
    """
    client = isolated_app.app.test_client()
    elsewhere = str(tmp_path / 'other_disk' / 'base_z8')

    resp = client.put('/api/config', json={'terrain_global_base_path': elsewhere})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert ConfigManager().get('terrain_global_base_path') == elsewhere
