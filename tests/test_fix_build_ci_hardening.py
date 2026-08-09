"""构建 / CI 加固回归（2026-08-08 全量评审 P1#12、P1#13 与四项基础设施 P2）。

这个文件大半是配置文件断言 —— 因为缺陷本身就在配置里，且它们只在真实发版那次
才会显形（打包残留进 Release、上游 Nuitka 一发新版就炸、conda 装失败报绿）。
所以每条断言都尽量做成**行为级**：清理步骤是把 shell 片段抠出来真跑一遍，
shell 标志位是拿真 bash 跑一遍看 errexit/pipefail 有没有生效，`.gitattributes`
是问 `git check-attr`，哨兵校验是真喂一棵缺文件的假产物树。

覆盖的旧行为（每一条在修复前都是红的）：
- 清理步骤只删 data/downloads/cache + smoke.log，且事后只断言 data 还在不在。
  冒烟测试那一次真启动还会留下 logs/terraforge.log（内容带 CI runner 路径）和
  assets/terrain/{base_z8, .base_unpack_<pid>_*}（Nuitka 排掉了 base_z8 却打进
  分卷 → is_base_ready 恒假 → 后台线程开始解 4.3 万个文件，进程被 kill 时留下
  半棵树）。tar/7z 随后把它们原样打进发布包。
- nuitka 与 matplotlib 是全项目唯二没有版本约束的依赖，而 nuitka_build.py 调的
  是 nuitka.freezer 的私有 API。
- `shell: bash -l {0}` 整串替换掉了 GitHub 默认的 `bash --noprofile --norc -eo
  pipefail {0}`，多命令 step 于是只由最后一条命令定成败。
- 只校验 gdal-data/proj-data 落地，不校验 templates/static。
- `.gitattributes` 没有覆盖仓库里最大的二进制（base 地形分卷）。
- 第三方 action 用可变 tag，且两个 workflow 都没有 permissions: 块。
"""

import ast
import os
import re
import shutil
import subprocess
import sys

import pytest

from conftest import REAL_BASH, needs_bash

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

BUILD_WF = os.path.join(".github", "workflows", "build.yml")
TEST_WF = os.path.join(".github", "workflows", "test-build.yml")


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _step_script(wf_rel, step_name):
    """把某个 step 的 `run: |` 块抠出来并去掉 YAML 缩进，得到可执行的 shell。"""
    lines = _read(wf_rel).splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"- name: {step_name}")
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for ln in lines[run_at + 1:]:
        if ln.strip().startswith("- name:"):
            break
        if ln.strip() == "":
            body.append("")
            continue
        if not ln.startswith(" " * 8):
            break
        body.append(ln[8:])
    assert body, f"{step_name} 的 run 块是空的"
    return "\n".join(body) + "\n"


# ---------------------------------------------------------------------------
# P1#12：发布包不得携带冒烟测试残留
# ---------------------------------------------------------------------------

CLEANUP_STEP = "Strip smoke-test runtime data before packaging"

# 冒烟测试那一次真启动会造出来的东西，以及**必须活下来**的随包数据。
_RESIDUE = (
    "data/map_downloader.db",
    "downloads/keep-me.tif",
    "cache/tiles/1/2/3.png",
    "logs/terraforge.log",
    "smoke.log",
    "assets/terrain/base_z8/layer.json",
    "assets/terrain/base_z8/0/0/0.terrain",
    "assets/terrain/.base_unpack_34971_oeg4o9m2/base_z8/layer.json",
    "assets/terrain/.base_unpack.lock",
)
_SHIPPED = (
    "terraforge",
    "templates/index.html",
    "static/vendor/fonts/fonts.css",
    "assets/terrain/base_z8.tar.gz.partaa",
    "assets/terrain/base_z8.tar.gz.partab",
    "gdal-data/epsg.wkt",
)


def _fake_dist(tmp_path):
    root = tmp_path / "dist" / "terraforge"
    for rel in _RESIDUE + _SHIPPED:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return root


def _run_cleanup(script, cwd):
    # 必须用 conftest.find_real_bash 找出来的那个 bash：Windows runner 上裸
    # `bash` 命中的是 System32 的 WSL 占位 stub，它只用 UTF-16 打一句
    # 「no installed distributions」并非零退出，用例全红且报错看不出原因。
    return subprocess.run([REAL_BASH, "-eo", "pipefail", "-c", script],
                          cwd=str(cwd), capture_output=True, text=True, timeout=60)


@needs_bash
def test_cleanup_removes_every_kind_of_smoke_residue(tmp_path):
    """真跑清理脚本：残留全清，随包数据一个不少。

    旧脚本在 logs/、base_z8/、.base_unpack_* 这三项上留货 —— 而它们随后被
    tar/7z 打进用户下载的包（日志里是 CI runner 的路径；半棵 base 树没有任何
    运行期路径会回收）。
    """
    dist = _fake_dist(tmp_path)
    proc = _run_cleanup(_step_script(BUILD_WF, CLEANUP_STEP), tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    for rel in _RESIDUE:
        assert not (dist / rel).exists(), f"残留未清除: {rel}"
    assert not (dist / "logs").exists(), "logs 目录本身也要删掉"
    assert not list((dist / "assets" / "terrain").glob(".base_unpack_*")), \
        "pid 命名的解压临时目录还在"
    for rel in _SHIPPED:
        assert (dist / rel).exists(), f"清理把随包数据也删了: {rel}"


@pytest.mark.parametrize("residue_token", [
    "data", "downloads", "cache", "logs",
    "assets/terrain/base_z8", ".base_unpack_",
])
@needs_bash
def test_cleanup_assertion_catches_whatever_the_rm_lines_missed(tmp_path, residue_token):
    """删掉某一类残留的 rm 行后，脚本必须**红**。

    这条钉的是「事后断言」而不是 rm 本身：以后再多一类残留、有人只加 rm 不加
    断言，或者反过来改 rm 时漏一项，都要在 CI 里当场炸，而不是静默打进包。
    旧脚本的事后断言只有一句 `if [ -d dist/terraforge/data ]`，除了 data 之外
    任何一项漏删都照样报绿 —— 所以这条用例在旧脚本上除 data 外全红。
    """
    script = _step_script(BUILD_WF, CLEANUP_STEP)
    kept = [ln for ln in script.splitlines()
            if not (ln.strip().startswith("rm ") and residue_token in ln)]
    assert len(kept) < len(script.splitlines()), f"没找到删 {residue_token} 的 rm 行"

    _fake_dist(tmp_path)
    proc = _run_cleanup("\n".join(kept) + "\n", tmp_path)
    assert proc.returncode != 0, (
        f"漏删 {residue_token} 却报绿 —— 事后断言没覆盖它\n" + proc.stdout)
    assert "FAILED" in proc.stdout + proc.stderr


def test_cleanup_still_runs_between_smoke_test_and_packaging():
    """顺序前提：清理必须夹在冒烟测试与打包之间，否则清了也白清。"""
    wf = _read(BUILD_WF)
    assert wf.index("- name: Smoke test executable") \
        < wf.index(f"- name: {CLEANUP_STEP}") \
        < wf.index("- name: Package application")


# ---------------------------------------------------------------------------
# P1#13：最容易炸构建的两个依赖必须钉住
# ---------------------------------------------------------------------------

def _requirements_specs():
    specs = {}
    for line in _read("requirements.txt").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~\s]", line, 1)[0]
        specs[name.lower()] = line
    return specs


def test_nuitka_is_pinned_exactly():
    """裸 `pip install nuitka` 拉最新 = 上游一发版，tag 推上去那次构建才炸。

    nuitka_build.py 调的是私有 API
    `nuitka.freezer.DllDependenciesWin32.detectBinaryPathDLLsWin32`（八个关键字
    参数），改签名对上游不算 breaking change。
    """
    spec = _requirements_specs().get("nuitka")
    assert spec, "requirements.txt 里没有 nuitka —— 构建期依赖也要钉"
    assert re.fullmatch(r"nuitka==\d+(\.\d+)*", spec), f"必须是精确钉，实际: {spec}"


def test_matplotlib_is_pinned_exactly():
    """matplotlib 既是等高线渲染的运行期依赖，又靠 --include-package-data 打进产物。"""
    spec = _requirements_specs().get("matplotlib")
    assert spec and re.fullmatch(r"matplotlib==\d+(\.\d+)*(\.post\d+)?", spec), \
        f"matplotlib 必须精确钉，实际: {spec}"


def test_gdal_stays_a_range_not_a_pin():
    """反向保护：GDAL【不能】精确钉（绑定是 sdist 现编，版本跟随机器）。

    见 requirements.txt 顶部注释：钉具体值会触发按钉的版本重编，而那一次没有
    --no-build-isolation，编出来的绑定缺 _gdal_array。
    """
    spec = _requirements_specs().get("gdal")
    assert spec == "GDAL>=3.8,<4", f"GDAL 那行被改了: {spec}"


@pytest.mark.parametrize("wf", [BUILD_WF, TEST_WF])
def test_workflows_install_nuitka_from_requirements(wf):
    """workflow 不许再 `pip install nuitka`（那会绕过 requirements.txt 的钉）。"""
    content = _read(wf)
    bare = re.findall(r"^\s*(?:python -m )?pip install nuitka\s*$", content, re.M)
    assert not bare, f"{wf} 仍在裸装 nuitka: {bare}"
    assert "pip install -r requirements.txt" in content \
        or "pip install -r /tmp/req.txt" in content, \
        f"{wf} 得从 requirements.txt 装依赖，nuitka 才拿得到钉住的版本"


@pytest.mark.parametrize("wf", [BUILD_WF, TEST_WF])
def test_compile_cache_key_is_scoped_to_the_nuitka_version(wf):
    """缓存 key 与**每一条** restore-key 都要带 Nuitka 版本。

    ccache 里的对象文件是 Nuitka 生成的 C 源编出来的，换版本就全不作数。
    只把版本加进 key 不够：最宽的那条 restore-key 会绕过 hashFiles，把旧版本
    留下的缓存拉回来。
    """
    content = _read(wf)
    key = re.search(r"^\s*key: (nuitka-.*)$", content, re.M)
    assert key, f"{wf} 找不到编译缓存的 key"
    assert "env.NUITKA_VERSION" in key.group(1), key.group(1)

    restores = re.search(r"^\s*restore-keys: \|\n((?:\s+nuitka-.*\n)+)", content, re.M)
    assert restores, f"{wf} 找不到 restore-keys"
    for line in restores.group(1).strip().splitlines():
        assert "env.NUITKA_VERSION" in line, f"restore-key 没锁版本: {line.strip()}"

    assert re.search(r"NUITKA_VERSION=.*GITHUB_ENV", content), \
        f"{wf} 没有任何一步把 NUITKA_VERSION 写进 $GITHUB_ENV，key 会展开成空串"


# ---------------------------------------------------------------------------
# P2：自定义 shell 不能丢掉 errexit / pipefail
# ---------------------------------------------------------------------------

def _default_shell_argv():
    m = re.search(r"^\s*shell: (.+)$", _read(BUILD_WF), re.M)
    assert m, "build.yml 没有 defaults.run.shell"
    argv = m.group(1).split()
    assert argv[-1] == "{0}", f"shell 末尾必须是脚本占位符: {m.group(1)}"
    # argv[0] 是 workflow 里写的字面量 "bash"。本地执行时必须换成 conftest 找出来
    # 的那个**真** bash：Windows runner 上裸 `bash` 命中 System32 的 WSL 占位 stub
    # （UTF-16 打一句 no installed distributions 后非零退出），断言会全红且看不出
    # 原因。校验的是 workflow 给的那些**标志位**，用哪个 bash 二进制不影响结论。
    return [REAL_BASH] + argv[1:-1]


@needs_bash
def test_default_shell_keeps_errexit():
    """拿真 bash 验：多命令 step 里前面的命令失败必须当场终止。

    `bash -l {0}` 覆盖了 GitHub 默认的 `bash --noprofile --norc -eo pipefail`，
    「Install GDAL (Windows/macOS)」以一句恒成功的 `echo >> $GITHUB_ENV` 收尾，
    conda 装失败会报绿，几步之后变成一个风马牛不相及的 ImportError。
    """
    argv = _default_shell_argv()
    proc = subprocess.run(argv + ["-c", "false\necho REACHED_THE_END"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, "shell 没开 errexit：失败的中间命令不会让 step 变红"
    assert "REACHED_THE_END" not in proc.stdout


@needs_bash
def test_default_shell_keeps_pipefail():
    """管道中段失败也要算失败（`conda install ... | tee` 这类写法的唯一保障）。"""
    argv = _default_shell_argv()
    proc = subprocess.run(argv + ["-c", "set +e; false | true; echo rc=$?"],
                          capture_output=True, text=True, timeout=60)
    assert "rc=1" in proc.stdout, f"shell 没开 pipefail: {proc.stdout!r}"


def test_default_shell_is_still_a_login_shell():
    """反向保护：-l 不能被顺手删掉 —— conda 环境靠它进 PATH。"""
    assert "-l" in "".join(_default_shell_argv()[1:]), _default_shell_argv()


# ---------------------------------------------------------------------------
# P2：token 权限与第三方 action 的可变 tag
# ---------------------------------------------------------------------------

def test_release_action_is_pinned_to_a_commit_sha():
    """第三方 action 拿着 GITHUB_TOKEN，可变 tag 能被上游重新指向。"""
    wf = _read(BUILD_WF)
    uses = re.findall(r"uses: (softprops/action-gh-release@\S+)", wf)
    assert uses, "build.yml 里找不到发布用的 action"
    for ref in uses:
        assert re.fullmatch(r"softprops/action-gh-release@[0-9a-f]{40}", ref), \
            f"必须钉到 40 位 commit SHA，实际: {ref}"


def test_build_job_declares_only_the_write_permission_it_needs():
    """没有 permissions: 块时 GITHUB_TOKEN 按仓库默认档（常见 write-all）签发。"""
    wf = _read(BUILD_WF)
    m = re.search(r"^    permissions:\n((?:      \S+: \S+\n)+)", wf, re.M)
    assert m, "build 这个 job 没有声明 permissions:"
    granted = dict(ln.strip().split(": ") for ln in m.group(1).strip().splitlines())
    assert granted == {"contents": "write"}, \
        f"只该要 contents: write（发 Release），实际: {granted}"


def test_test_build_workflow_is_read_only():
    """test-build.yml 没有任何一步需要写权限。"""
    m = re.search(r"^permissions:\n((?:  \S+: \S+\n)+)", _read(TEST_WF), re.M)
    assert m, "test-build.yml 没有声明 permissions:"
    granted = dict(ln.strip().split(": ") for ln in m.group(1).strip().splitlines())
    assert granted == {"contents": "read"}, granted


# ---------------------------------------------------------------------------
# P2：.gitattributes 漏掉了仓库里最大的二进制
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 才能问 check-attr")
@pytest.mark.parametrize("part", ["partaa", "partab"])
def test_base_terrain_volumes_are_declared_binary(part):
    """分卷此前只靠 git 的 NUL 字节启发式活着。

    文档写明的装机流程是 `cat base_z8.tar.gz.part* > base_z8.tar.gz` 再交给
    scripts/unpack_base_terrain.py —— 字节级完整性是这条流程的前提，而
    `* text=auto eol=lf` 一旦把它判成文本就会做行尾转换。
    """
    rel = f"assets/terrain/base_z8.tar.gz.{part}"
    proc = subprocess.run(["git", "check-attr", "binary", "text", "--", rel],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert f"{rel}: binary: set" in proc.stdout, proc.stdout
    assert f"{rel}: text: unset" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# P2：只校验 GDAL/PROJ 数据落地，不校验自家的 templates/static
# ---------------------------------------------------------------------------

import nuitka_build  # noqa: E402


def test_app_data_sentinels_exist_in_the_source_tree():
    """哨兵必须指向真实存在的文件，否则它只是一句永远失败的空话。"""
    for pat in nuitka_build.APP_DATA_SENTINELS:
        import glob as _glob
        hits = _glob.glob(os.path.join(PROJECT_ROOT, *pat.split("/")))
        assert hits, f"哨兵 {pat} 在源码树里就不存在"


def test_verify_app_data_reports_every_missing_sentinel(tmp_path):
    """漏收 static/ 的产物能启动、`/` 也照样 200（只用到 templates/），
    用户看到的是一张白地图且没有任何报错 —— 必须在构建期就红。"""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        nuitka_build.verify_app_data(str(tmp_path))
    msg = str(exc.value)
    assert "Cesium.js" in msg and "fonts.css" in msg, \
        f"缺什么要一次全列出来，实际: {msg}"
    assert "index.html" not in msg, "在位的文件不该被列成缺失"


def test_verify_app_data_accepts_a_complete_bundle(tmp_path):
    """对照：哨兵齐全就放行（Cesium 目录带版本号、base 分卷带 part 后缀，靠 glob 匹配）。"""
    for rel in ("templates/index.html",
                "static/vendor/cesium/1.143.0/Cesium.js",
                "static/vendor/fonts/fonts.css",
                "assets/terrain/base_z8.tar.gz.partaa",
                "assets/terrain/base_z8.tar.gz.partab"):
        p = tmp_path.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    nuitka_build.verify_app_data(str(tmp_path))


def test_build_actually_calls_verify_app_data():
    """挂上去才算数：main() 里必须真的调它（且在重命名之后，路径才对）。"""
    src = _read("nuitka_build.py")
    main_at = src.index("def main():")
    body = src[main_at:]
    assert body.index("os.rename(src, dst)") < body.index("verify_app_data(dst)"), \
        "必须在 dist/app.dist → dist/terraforge 改名之后校验"


# ---------------------------------------------------------------------------
# P2：freeze_support() 要么真生效，要么别号称自己是拦截点
# ---------------------------------------------------------------------------

def _module_level_calls(rel):
    """按出现顺序列出模块级的裸函数调用名。"""
    tree = ast.parse(_read(rel))
    names = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name):
            names.append(node.value.func.id)
    return names


def test_bundle_env_is_set_up_before_the_entry_guards():
    """CPython 的 freeze_support() 在 win32 上要求 sys.frozen 为真才做事，
    而 sys.frozen 是 bundle_dir() 设的（Nuitka 自己不设）。

    旧顺序是 install_entry_guards() 在前 —— 于是 Windows 打包产物里那个
    freeze_support() 什么都不做，注释却称它是「frozen 下真正有效的拦截点」。
    """
    calls = _module_level_calls("app.py")
    assert "setup_bundle_env" in calls and "install_entry_guards" in calls
    assert calls.index("setup_bundle_env") < calls.index("install_entry_guards"), \
        f"顺序反了: {calls}"


@pytest.mark.parametrize("rel", ["src/core/bundle.py", "src/core/process_entry.py"])
def test_the_two_modules_ahead_of_the_guard_import_only_stdlib(rel):
    """上面那个换序的安全前提：这两个模块都不许碰 GDAL / 本项目的重量级模块。

    一旦谁在这里加了 `from src.services...` 或 `import osgeo`，守卫「必须赶在
    重量级 import 之前」这条前提就没了，而症状是 frozen worker 重跑 app 初始化
    把运行中的任务改判 —— 排查成本极高，所以在这里钉住。
    """
    allowed = {"os", "sys", "multiprocessing"}
    tree = ast.parse(_read(rel))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, f"{rel} 多了 {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in allowed, \
                f"{rel} 多了 from {node.module}"


def test_freeze_support_comment_no_longer_claims_to_be_the_interception_point():
    """真正拦住 frozen worker 的是 runtime_mode 里的 `__parents_main__`：
    Nuitka 的 C bootstrap 自己扫 argv 里的 --multiprocessing-fork，worker 走不到
    Python 层的 __main__，freeze_support() 那一行根本不在路径上。"""
    src = _read("src/core/process_entry.py")
    guard_at = src.index("def install_entry_guards")
    body = src[guard_at:]
    assert "__parents_main__" in body, "注释必须点明真正的拦截点在哪"
    assert "真正有效的拦截点" not in body, "旧注释把 no-op 说成了唯一有效的拦截点"
