"""文档里**可机检的事实**，钉住，防止再次静默烂掉。

2026-08-08 评审的「文档一致性」一节抓到的都是这类：README 的 API 一节漏了 8 条现役
路由（含每个页面都依赖的 `/basemap/<z>/<x>/<y>`）、`CLAUDE.md` 全文 grep `basemap`
零命中（两条最容易被下一个人破坏的硬约束只活在一个源码注释和两个测试里）、
`INSTALL.md` 让读者去改一行 `requirements.txt` 里根本不存在的 `GDAL==3.8.4`。
这些都不是「写得不好」，是**读者照做会出事**，而且每一条都能用代码本身对账。

为什么朴素断言是空的：
  · 「README 里出现过 `/basemap`」—— 加一句散文就能满足，漏掉一条路由照样绿。
    真正的判据是**双向**：README 记的每条路由必须在 `src/routes/` 里存在，
    且 `src/routes/` 里的每条路由必须被 README 记到。少了后一半就抓不到那 8 条。
  · 「CLAUDE.md 里有 basemap 这个词」—— 同样一句散文就能满足。判据是那两条约束
    各自的**标识符**（GCJ-02 与被禁的两家、`client_descriptor` 与同源路径），
    以及两个源文件路径都被指出来。
  · 「文档里没有 `GDAL==`」—— 会误伤合法的 `pip install "GDAL==$(gdal-config --version)"`。
    判据是：凡是把某个 GDAL 版本表达式**归给 `requirements.txt`** 的那一行，
    表达式必须与 `requirements.txt` 里实际那一行逐字相同（旧的 `GDAL==3.8.4` 就是
    这么错的）；以及凡是描述构建闸门的文档，必须指名当前的闸门 `check_gdal.py`。

断言全部落在**路径与标识符**上，不落在句子上——散文可以随便重写。
"""

import ast
import os
import re

from src.i18n.catalog import MESSAGES

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# README 的端点清单只覆盖 HTTP 路由；socketio_events.py 没有 route 装饰器，
# 它的事件名单独列在 README 的「WebSocket 事件」一节。
ROUTES_DIR = os.path.join(PROJECT_ROOT, "src", "routes")

README = os.path.join(PROJECT_ROOT, "README.md")
CLAUDE_MD = os.path.join(PROJECT_ROOT, "CLAUDE.md")

# 归属表见 docs/README.md「谁负责写什么」：这些是会被读者照着执行的文档。
GDAL_DOCS = (
    "README.md",
    "CLAUDE.md",
    os.path.join("docs", "README.md"),
    os.path.join("docs", "guides", "README.md"),
    os.path.join("docs", "guides", "INSTALL.md"),
    os.path.join("docs", "guides", "BUILD.md"),
    os.path.join("docs", "guides", "QUICKSTART.md"),
)


def _read(rel_or_abs):
    path = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(PROJECT_ROOT, rel_or_abs)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _normalise_rule(rule):
    """`/api/tasks/<int:task_id>` 与 README 的 `/api/tasks/<id>` 归一到同一个键。

    参数名/转换器是实现细节，文档没有义务复述；位置和段数才是契约。
    查询串（README 的 `?path=<绝对路径>`）不属于路由本身，剥掉。
    """
    rule = rule.split("?", 1)[0]
    rule = re.sub(r"<[^>]*>", "<>", rule)
    if len(rule) > 1:
        rule = rule.rstrip("/")
    return rule


def _collect_code_routes():
    """AST 扫 `src/routes/*.py`，返回 {(method, 归一化路径)}。

    用 AST 而不是正则：`methods=` 可能换行、可能用单引号也可能用双引号，
    正则漏一种写法就会让棘轮悄悄松掉一半。
    """
    found = set()
    for name in sorted(os.listdir(ROUTES_DIR)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(_read(os.path.join(ROUTES_DIR, name)), filename=name)

        prefixes = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if not (isinstance(func, ast.Name) and func.id == "Blueprint"):
                continue
            prefix = ""
            for kw in node.value.keywords:
                if kw.arg == "url_prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value or ""
            for target in node.targets:
                if isinstance(target, ast.Name):
                    prefixes[target.id] = prefix

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                attr = dec.func
                if not (isinstance(attr, ast.Attribute) and attr.attr == "route"):
                    continue
                if not (isinstance(attr.value, ast.Name) and attr.value.id in prefixes):
                    continue
                if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                    continue
                rule = prefixes[attr.value.id] + dec.args[0].value
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                for method in methods:
                    found.add((method.upper(), _normalise_rule(rule)))
    return found


def _readme_h2_section(title):
    """按标题取 README 的一个 H2 小节正文（不含标题行本身）。

    标题允许带 emoji 前缀（`## 🔌 API 端点`）—— 这些断言盯的是小节**内容**，
    不是标题的字面写法；写死 `"\n## API 端点\n"` 会让 README 加一个图标就全红。
    """
    text = _read(README)
    m = re.search(rf"^##[^\n]*?{re.escape(title)}\s*$", text, re.M)
    assert m, f"README 找不到「{title}」一节"
    rest = text[m.end():]
    end = rest.find("\n## ")
    return rest[:end] if end != -1 else rest


_DOC_ENDPOINT = re.compile(r"^-\s+`([A-Z|]+)\s+(/\S*?)`")


def _collect_readme_routes():
    """README「## API 端点」一节里的端点条目，返回 {(method, 归一化路径)}。"""
    section = _readme_h2_section("API 端点")

    found = set()
    for line in section.splitlines():
        m = _DOC_ENDPOINT.match(line.strip())
        if not m:
            continue
        for method in m.group(1).split("|"):
            found.add((method.upper(), _normalise_rule(m.group(2))))
    return found, section


def test_readme_documents_every_live_route():
    """这一条就是当初能抓到那 8 条漏记路由的断言（含 `/basemap/<z>/<x>/<y>`）。"""
    code = _collect_code_routes()
    documented, _ = _collect_readme_routes()
    missing = sorted(code - documented)
    assert not missing, (
        "src/routes/ 里存在但 README「API 端点」一节没记的路由：\n  "
        + "\n  ".join(f"{m} {p}" for m, p in missing))


def test_readme_documents_no_dead_route():
    """反向：README 记的端点必须真的在 `src/routes/` 里存在。

    没有这一半，删掉一条路由（0.2.12 就删了四条 `/cancel`）而忘了改 README，
    读者会照着一份幽灵清单写客户端。
    """
    code = _collect_code_routes()
    documented, _ = _collect_readme_routes()
    ghosts = sorted(documented - code)
    assert not ghosts, (
        "README「API 端点」一节记了、但 src/routes/ 里不存在的路由：\n  "
        + "\n  ".join(f"{m} {p}" for m, p in ghosts))


def test_readme_marks_basemap_as_the_mandatory_same_origin_hop():
    """`/basemap` 不能只是清单里的一行——它是**强制**的一跳，理由必须写在旁边。"""
    _, section = _collect_readme_routes()
    line = next((l for l in section.splitlines() if "/basemap/<z>/<x>/<y>" in l), None)
    assert line, "README 的 API 一节找不到 /basemap/<z>/<x>/<y>"
    for token in ("同源", "CORS", "proxy_url"):
        assert token in line, (
            f"/basemap 那条缺少「为什么这一跳是强制的」里的 {token}：{line}")


def test_claude_md_carries_the_two_basemap_invariants():
    """`CLAUDE.md` 曾经 grep `basemap` 零命中，而这两条约束只活在源码注释和两个测试里。

    断言落在标识符上：约束一是坐标系（GCJ-02 / 高德 / 腾讯），约束二是
    「上游地址不出服务端」（`client_descriptor` 剥掉 upstream + 同源路径），
    外加两个源文件路径，好让读者能顺着找过去。
    """
    text = _read(CLAUDE_MD)
    assert "basemap" in text.lower(), "CLAUDE.md 全文没有 basemap"

    for path in ("src/services/basemap_source.py", "src/routes/basemap_static.py"):
        assert path in text, f"CLAUDE.md 没有指向 {path}"

    # 约束一：禁 GCJ-02 源。
    assert "GCJ-02" in text, "CLAUDE.md 没写坐标系约束（GCJ-02）"
    assert "Gaode" in text or "高德" in text, "CLAUDE.md 没点名被禁的高德"
    assert "Tencent" in text or "腾讯" in text, "CLAUDE.md 没点名被禁的腾讯"

    # 约束二：上游 URL 不出服务端。
    assert "client_descriptor" in text, "CLAUDE.md 没提剥掉 upstream 的 client_descriptor"
    assert "/basemap/{z}/{x}/{y}" in text, "CLAUDE.md 没写浏览器实际看到的同源路径"

    # 转发路由落地之后底图【吃】proxy_url——仓里几处注释曾声称相反。
    assert "proxy_url" in text, "CLAUDE.md 没写底图与下载共用 proxy_url"


def _requirements_gdal_spec():
    """`requirements.txt` 里 GDAL 那一行的实际写法，例如 `GDAL>=3.8,<4`。"""
    m = re.search(r"^GDAL[^\s#]*", _read("requirements.txt"), re.MULTILINE)
    assert m, "requirements.txt 里找不到 GDAL 依赖行"
    return m.group(0)


# 带**数字**的版本表达式才算「一个具体的版本主张」；`"GDAL==$(gdal-config --version)"`
# 这种 shell 展开不算，它是合法的安装命令。
_VERSION_CLAIM = re.compile(r"GDAL\s*(?:==|>=|<=|!=|~=|<|>)\s*\d[\d.,<>=!~]*")


def test_no_doc_misquotes_the_requirements_gdal_spec():
    """凡是把某个 GDAL 版本表达式归给 `requirements.txt` 的行，必须逐字引对。

    旧的 `docs/guides/INSTALL.md` 让读者去改 `requirements.txt` 里的 `GDAL==3.8.4`——那行
    从来不存在（实际是范围 `GDAL>=3.8,<4`，因为绑定是 sdist 现编、版本跟随机器）。
    同一处漂移也是 `./build.sh` 静默 exit 1 的成因：两个脚本查的是这个不存在的钉。
    """
    actual = _requirements_gdal_spec().replace(" ", "")
    offenders = []
    for rel in GDAL_DOCS:
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            if "requirements.txt" not in line:
                continue
            for claim in _VERSION_CLAIM.findall(line):
                if claim.replace(" ", "").rstrip(",") != actual:
                    offenders.append(f"{rel}:{lineno} 声称 {claim!r}，实际是 {actual!r}")
    assert not offenders, "文档与 requirements.txt 的 GDAL 声明不一致：\n  " + "\n  ".join(offenders)


def test_docs_that_describe_the_build_gate_name_the_current_gate():
    """描述 `build.sh` GDAL 闸门的文档必须指名 `scripts/check_gdal.py`。

    闸门 2026-08-08 从「查 requirements.txt 里的 `GDAL==` 精确钉」换成了
    「版本落在声明范围内 + `_gdal_array` 在位」，判据只有那一个文件。文档要是继续
    描述旧判据，读者会去 requirements.txt 里加一行钉——那正是当初把三个平台的
    绑定装坏的做法。
    """
    gate = "check_gdal.py"
    offenders = []
    for rel in GDAL_DOCS:
        text = _read(rel)
        if "build.sh" in text and "GDAL" in text and gate not in text:
            offenders.append(rel)
    assert not offenders, (
        f"这些文档谈到 build.sh 与 GDAL 却没有指名当前闸门 {gate}：{offenders}")


def test_no_doc_ties_a_gdal_pin_to_requirements_txt():
    """`GDAL==` 与 `requirements.txt` 不许出现在同一行——含否定句。

    上一条只抓「引错了版本号」，抓不到不带数字的主张（「requirements.txt 需要
    `GDAL==` pin」）。而 `GDAL==` 在文档里唯一的合法用途是安装命令
    （`uv pip install --no-build-isolation "GDAL==$(gdal-config --version)"`），
    那种行不会提 `requirements.txt`。所以「同一行出现两者」这个形状本身就是错的，
    连「里面没有 `GDAL==` 这样一行」这种澄清也请分行写——否则下一次 grep 的人
    只看见半句，又会去把钉加回去（这正是 `./build.sh` 静默 exit 1 的来源）。
    """
    offenders = [
        f"{rel}:{lineno}"
        for rel in GDAL_DOCS
        for lineno, line in enumerate(_read(rel).splitlines(), 1)
        if "GDAL==" in line and "requirements.txt" in line
    ]
    assert not offenders, "把 `GDAL==` 钉与 requirements.txt 写在同一行：" + str(offenders)


def test_claude_md_testing_rules_match_the_real_conftest():
    """`CLAUDE.md` 曾写「no `conftest.py`」并让人在测试文件顶部 `sys.path.insert`。

    `tests/conftest.py` 一直存在，且它自己的 docstring 要求新测试走
    `fresh_import()` / `isolated_app`。照那句旧规约写出来的，正是
    `test_conftest_isolation_contract.py` 存在的目的所要收紧的东西。

    断言两侧都锚在真实符号上：CLAUDE.md 必须点名这三个标识符，而这三个必须
    真的在 `tests/conftest.py` 里定义——只查文档就会在工具改名后一起烂掉。
    """
    conftest_src = _read(os.path.join("tests", "conftest.py"))
    defined = {
        node.name
        for node in ast.walk(ast.parse(conftest_src))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for symbol in ("fresh_import", "isolated_app"):
        assert symbol in defined, f"tests/conftest.py 里没有 {symbol}，文档规约无处可指"

    section = _read(CLAUDE_MD).split("### Testing patterns to follow", 1)
    assert len(section) == 2, "CLAUDE.md 找不到「Testing patterns to follow」一节"
    rules = section[1]
    for symbol in ("conftest.py", "fresh_import", "isolated_app",
                   "test_conftest_isolation_contract.py"):
        assert symbol in rules, f"CLAUDE.md 的测试规约没有点名 {symbol}"
    assert "no `conftest.py`" not in rules, (
        "CLAUDE.md 又写回了「没有 conftest.py」——它有，而且是隔离设施的入口")


def _readme_structure_tree():
    return _readme_h2_section("项目结构").split("```")[1]


# 运行时才创建、且被 .gitignore 排除的目录。它们**应该**出现在结构树里（用户第一次
# 启动后就会看到），但在干净 clone / CI checkout 里并不存在 —— 所以「必须真的存在」
# 那条断言要放它们过去。
#
# 这个豁免本身有闸：下面 test_runtime_dirs_exemption_is_not_a_hole 断言每一项都真的
# 被 .gitignore 排除。否则豁免就是个洞 —— 拼错一个名字、或者某个真目录被误删，
# 都会被它悄悄吞掉。（本条正是 CI 抓到的：开发机上这四个目录都在，本地全绿，
# 到干净 checkout 才红。）
_RUNTIME_DIRS = frozenset({"data", "downloads", "cache", "logs"})


def test_readme_structure_tree_lists_the_real_top_level_dirs():
    """结构树里出现的每个仓内路径都必须真的存在。

    上一版是 2026-08-04 的逐文件快照，四天后已经漏掉整个 `src/i18n/`、
    `src/app_factory.py` 与 `src/core/` 一半的文件。现在改成按目录列，
    这条断言管的是另一半：列出来的东西不许是幻觉（下一条管漏。）
    """
    block = _readme_structure_tree()

    # 树是有层级的：`src/` 下缩进一格的 `core/` 指的是 `src/core`，不是仓根的 `core`。
    # 缩进宽度固定 4（`├── ` / `│   `），按它维护一个父路径栈。
    parents = {}
    missing = []
    for line in block.splitlines():
        body = line.split("#", 1)[0]
        stripped = re.sub(r"^[\s│├└─]*", "", body)
        if not stripped.strip():
            continue
        depth = (len(body) - len(stripped)) // 4
        entry = stripped.strip()
        if entry == "map-download/":
            parents = {0: ""}
            continue
        # 一行可能并列多个条目：`build.sh / build.bat`、`data/ downloads/ cache/ logs/`。
        tokens = [t.strip().rstrip("/") for t in re.split(r"\s+/\s+|\s+", entry)]
        tokens = [t for t in tokens if t and t != "/"]
        parent = parents.get(depth, "")
        # 目录条目（以 / 结尾）才能当下一层的父；并列多项时不该有子层，取第一个即可。
        if entry.endswith("/") or len(tokens) == 1:
            parents[depth + 1] = os.path.join(parent, tokens[0]) if tokens else parent
        for token in tokens:
            rel = os.path.join(parent, token)
            if rel in _RUNTIME_DIRS:
                continue
            if not os.path.exists(os.path.join(PROJECT_ROOT, rel)):
                missing.append(f"{rel!r}（来自 {entry!r}）")
    assert not missing, "README 项目结构树里不存在的条目：\n  " + "\n  ".join(missing)


def test_runtime_dirs_exemption_is_not_a_hole():
    """上面那条豁免的每一项都必须（1）确实不会被 checkout 建出来，（2）确实还在树里。

    没有这道闸，`_RUNTIME_DIRS` 就是一张万能通行证：写错一个名字、或者哪天某个
    真目录被删掉而树里还留着，都会被静默放过 —— 而那条断言存在的全部意义就是
    「树里列的东西不许是幻觉」。

    判据是「没有任何被 git 跟踪的文件」而不是「在 .gitignore 里」：`data/` 只
    忽略了内容（`data/*.db`），目录本身没写进 .gitignore，但 git 不跟踪空目录，
    所以干净 checkout 里照样没有它 —— 「没有跟踪文件」才是让豁免成立的那个性质。
    第二条（必须出现在树里）负责让豁免不会变成陈旧名单。
    """
    import subprocess

    block = _readme_structure_tree()
    for name in sorted(_RUNTIME_DIRS):
        tracked = subprocess.run(
            ["git", "ls-files", "--", name],
            cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
        assert not tracked, (
            f"{name}/ 有被跟踪的文件（{tracked.splitlines()[:3]}），"
            "干净 checkout 里就会存在，不该豁免")
        assert re.search(rf"(^|[\s│├└─]){re.escape(name)}/", block, re.M), (
            f"{name}/ 已经不在 README 结构树里了 —— 豁免名单该同步删掉这一项")


def test_readme_structure_tree_names_the_real_composition_root():
    """树里必须有 `src/i18n` 与 `src/app_factory.py`，且「组合根」这三个字挂在后者身上。

    旧树两处都没有，还在 `app.py` 那行写着「组合根」。`app.py` 只是排启动时序，
    真正构造管理器、注入并注册蓝图的是 `app_factory.create_app()`（`CLAUDE.md` 一直
    是对的，README 与它矛盾）。照旧树理解的人会把新蓝图注册写进 `app.py`。
    """
    block = _readme_structure_tree()
    for token in ("src/", "i18n", "app_factory.py"):
        assert token in block, f"README 项目结构树里没有 {token}"

    root_lines = [l for l in block.splitlines() if "组合根" in l]
    assert root_lines, "结构树没有标出组合根"
    for line in root_lines:
        assert "app_factory.py" in line, (
            f"「组合根」标错了位置——它属于 app_factory.py，不是这一行：{line.strip()}")


# --------------------------------------------------- 界面文案里的可机检事实
#
# 底图瓦片由 `routes/basemap_static` 在**服务端**取，代理经
# `proxy_autodetect.resolve_from_config` 解析 —— 与下载完全同一条出网路径。
# 配置页的 `tpl.config.download.basemap_hint` 曾经中英两版都反着说（「底图由
# 浏览器直连加载，不经过代理设置」），而「底图打不开、只剩蓝球」是本仓最高频的
# 现场问题：用户照着这句话认定代理与底图无关，于是跳过了唯一能修好它的那一步。
#
# 为什么不能只 grep 「直连」：`js.config.proxy.none` 的「当前为直连」、
# `js.config.proxy.disabled` 的「留空即为直连」说的是「没有代理在用」，
# `tpl.config.download.basemap_esri` 的「国内直连可用」说的是上游本身可达 ——
# 三条都对。判据只能是「底图」与「绕过代理」这两个意思落在**同一句**里。
_SENTENCE = r"[^。；;.\n]"

_BASEMAP_SKIPS_PROXY = (
    # 底图……不经过/不走/不吃/不使用/不受……代理（及反序）
    rf"(?:底图|basemap){_SENTENCE}*?不(?:经过|走|吃|使用|受){_SENTENCE}*?代理",
    rf"不(?:经过|走|吃|使用|受){_SENTENCE}*?代理{_SENTENCE}*?(?:底图|basemap)",
    # 底图……浏览器……直连（及反序）——旧文案的 zh 半句
    rf"(?:底图|basemap){_SENTENCE}*?浏览器{_SENTENCE}*?直连",
    rf"浏览器{_SENTENCE}*?直连{_SENTENCE}*?(?:底图|basemap)",
    # 代理……对底图……无效（及反序）
    rf"代理{_SENTENCE}*?(?:底图|basemap){_SENTENCE}*?无效",
    rf"(?:底图|basemap){_SENTENCE}*?代理{_SENTENCE}*?无效",
    # basemap … does not / never … go through|use|obey|honour … proxy
    rf"basemap{_SENTENCE}*?(?:does not|doesn't|do not|don't|never|is not|are not)"
    rf"{_SENTENCE}*?(?:go(?:es)? through|use|obey|respect|honou?r){_SENTENCE}*?prox",
    # basemap … loaded/fetched/served … browser … direct（及反序）——旧文案的 en 半句
    rf"basemap{_SENTENCE}*?browser{_SENTENCE}*?direct",
    rf"browser{_SENTENCE}*?direct{_SENTENCE}*?basemap",
    # basemap … bypass/skip/ignore … proxy
    rf"basemap{_SENTENCE}*?(?:bypass|skip|ignor)\w*{_SENTENCE}*?prox",
    # proxy … does not apply to / has no effect on … basemap
    rf"prox\w*{_SENTENCE}*?(?:does not|doesn't|has no|have no)"
    rf"{_SENTENCE}*?(?:apply|affect|effect){_SENTENCE}*?basemap",
)


def test_no_i18n_string_tells_the_user_the_basemap_skips_the_proxy():
    """整本文案目录里不许再出现「底图绕过代理」这个主张 —— 它是假的。

    扫的是合并后的 `MESSAGES`（zh 与 en 两栏都扫），不是某个文件的字面，
    这样把文案挪去别的 catalog 模块也逃不掉。
    """
    patterns = [re.compile(p, re.I) for p in _BASEMAP_SKIPS_PROXY]
    offenders = []
    for key, entry in MESSAGES.items():
        for loc, text in entry.items():
            hit = next((m for m in map(lambda p: p.search(text), patterns) if m), None)
            if hit:
                offenders.append(f"{key} [{loc}]: {hit.group(0)}")
    assert not offenders, (
        "这些文案又在说底图不吃代理——底图由 basemap_static 在服务端取，"
        "走 resolve_from_config，和下载同一条出网路径：\n  "
        + "\n  ".join(offenders))


def test_the_basemap_hint_states_the_same_origin_hop_and_the_proxy():
    """反向：上面那条只禁一种说法，空文案同样能满足它。

    这条钉住配置页必须**正面**告诉用户这一跳存在（应用内路径）以及代理管用 ——
    否则「底图打不开」的用户仍然不知道该去动哪个设置。同时保留另外两条仍然
    成立的事实：两个预设同为 WGS-84（框选位置对得上），以及被禁的 GCJ-02 源。
    """
    entry = MESSAGES["tpl.config.download.basemap_hint"]
    for loc, proxy_token in (("zh", "代理"), ("en", "prox")):
        text = entry[loc]
        assert "/basemap/{z}/{x}/{y}" in text, (
            f"basemap_hint[{loc}] 没写出浏览器实际请求的同源路径")
        assert proxy_token in text.lower(), (
            f"basemap_hint[{loc}] 没告诉用户代理设置对底图生效")
        for token in ("WGS-84", "GCJ-02"):
            assert token in text, f"basemap_hint[{loc}] 丢了 {token} 这条约束"



# ------------------------------------------------- 文档写出来的默认值 vs 代码
#
# 2026-08-09 评审在这一层抓到四条「照做会出事」：`CLAUDE.md` 说 `TileParams`
# 默认 `triangulator="auto"` / `normals=True`（实际是 `grid` / `False`）、说这几个
# 旋钮「不暴露给 UI/DB/API」（实际有两个配置键、两条路由在收）、
# `cesiumjs-loading.md` 把 `terrain_base_parent_url` 的默认值写成带 `/layer.json`
# 的形态（那正是 heightmap 陷阱本身：Cesium 拿 404 不报错，塞一个假 heightmap
# 图层污染共享 builder，实测 4154 m 山峰解成 −744 m），`INSTALL.md` 把 GDAL 钉成
# 一个具体版本（绑定是 sdist 现编，版本跟随机器）。
#
# 四条的共同点：读者照着做，全程零报错。所以判据一律是「文档里的字面量 ==
# 代码里解析出来的字面量」，用 AST 取值而不是 import —— 这个文件不该把 Flask、
# GDAL、数据库全拉起来。

# 与代码同步的文档。`docs/archive/` 与 `docs/reviews/` 不在其中：前者按约定保留
# 撰写当时的原貌（见 docs/reference/README.md「与 archive/ 的区别」），后者引用的
# 恰恰是修复前的错误原文。
_LIVE_DOC_DIRS = (
    os.path.join("docs", "reference"),
    os.path.join("docs", "guides"),
)
_LIVE_DOC_FILES = ("README.md", "CLAUDE.md", os.path.join("docs", "README.md"))


def _live_docs():
    rels = list(_LIVE_DOC_FILES)
    for rel_dir in _LIVE_DOC_DIRS:
        for root, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, rel_dir)):
            rels += [os.path.relpath(os.path.join(root, f), PROJECT_ROOT)
                     for f in files if f.endswith(".md")]
    return sorted(rels)


def _module_ast(rel):
    return ast.parse(_read(rel))


def _assigned(tree, name):
    """模块级 `NAME = <literal>` 的值。"""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return node.value
    raise AssertionError(f"找不到模块级赋值 {name}")


def _default_configs():
    """`src/core/database.py` 的 `DEFAULT_CONFIGS`，解析成 {key: value}。"""
    elts = _assigned(_module_ast(os.path.join("src", "core", "database.py")),
                     "DEFAULT_CONFIGS").elts
    return {e.elts[0].value: e.elts[1].value for e in elts}


def _tile_params_defaults():
    """`TileParams` 字段的默认值（只取字面量的那些）。"""
    tree = _module_ast(os.path.join("src", "services", "terrain_tiling",
                                    "dem_task_tiler.py"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "TileParams")
    return {n.target.id: n.value.value for n in cls.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Constant)}


_ABSOLUTE_URL = re.compile(r"https?://[^\s`)（）、，,]+")


def test_no_live_doc_misquotes_the_terrain_parent_url_default():
    """凡是提到 `terrain_base_parent_url` 的行，行内的 URL 必须就是它的默认值。

    `docs/reference/terrain/cesiumjs-loading.md` 曾把默认值写成
    `http://localhost:5000/terrain/base/layer.json`。那一节的主题恰恰是「什么时候
    必须改这个键」，照它手改配置或改任务 `layer.json` 的人会写出带 `/layer.json`
    的值 —— Cesium `appendForwardSlash()` 后再拼一次 `layer.json` 得 404，而它对
    这个 404 不报错：塞一个假 heightmap-1.0 图层，`heightmapStructure` 落在**共享**
    builder 上，任务自己的 quantized-mesh 瓦片也按 heightmap 解析。实测 4154 m
    山峰解成 −744 m，`hasVertexNormals` 仍报 true，瓦片全 200，控制台干净。

    只看「同一行里既提这个键又给了 URL」，不是全局禁 `/terrain/base/layer.json`
    —— 后者是 §1 里 base provider 自己的加载地址，完全合法。

    2026-08-10 默认值改成**应用内相对路径** `/terrain/base` 之后，判据也跟着变：
    这些行里允许出现绝对 URL（说明「指向另一套地形服务」时该怎么配，那是这个键
    现在唯一的用途），但**不允许**再出现应用自己的旧地址 `http://localhost:5000/...`
    —— 照那个值配，瓦片走 5001 专用 origin 时父级请求会绕回主连接池，远程访问
    时 `localhost` 更是指向客户端本机。带 `/layer.json` 的形态一律仍然禁止。
    """
    expected = _default_configs()["terrain_base_parent_url"]
    assert not expected.endswith("/layer.json"), (
        "DEFAULT_CONFIGS 里的 terrain_base_parent_url 自己就带上了 /layer.json")
    assert expected.startswith("/"), (
        "默认值应该是应用内相对路径（继承提供 layer.json 的 origin），"
        f"现在是 {expected!r}")

    offenders = []
    for rel in _live_docs():
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            if "terrain_base_parent_url" not in line:
                continue
            for url in _ABSOLUTE_URL.findall(line):
                if url.rstrip("/").lower().endswith("/layer.json"):
                    offenders.append(
                        f"{rel}:{lineno} 写的是 {url!r} —— 带 /layer.json 是 heightmap 陷阱")
                elif "//localhost:5000" in url or "//127.0.0.1:5000" in url:
                    offenders.append(
                        f"{rel}:{lineno} 写的是 {url!r} —— 应用自己的地址应写成相对路径 "
                        f"{expected!r}")
    assert not offenders, (
        "文档给 terrain_base_parent_url 写错了值：\n  " + "\n  ".join(offenders))


def test_distribution_security_section_documents_the_tile_port_cors():
    """瓦片端口给所有响应发 `Access-Control-Allow-Origin: *`，安全一节必须写明。

    这是 0.3 引入的**新能力**，且是安全一节里唯一一条与防火墙无关的：跨源读取
    来自用户自己浏览器里打开的任意页面，走回环地址，入站规则拦不到。发行文档
    的安全一节原本只讲「5000 面大、5001 面小」，读者据此判断放行 5001 的风险，
    却看不到「5001 上的东西任何网页都能读」。

    判据落在**代码事实**上而不是句子：只要 tile_server 仍然发这个头，文档就
    必须提到头名与它带来的跨源可读性；哪天真把头去掉了（瓦片就废了，但那是
    另一回事），这条断言自然让路。
    """
    tile_server_src = _read(os.path.join("src", "core", "tile_server.py"))
    if "Access-Control-Allow-Origin" not in tile_server_src:
        return                      # 头没了，这条文档要求也就不成立

    doc = _read(os.path.join("docs", "guides", "DISTRIBUTION.md"))
    security = doc[doc.index("### 防火墙警告"):doc.index("### macOS 安全警告")]
    assert "Access-Control-Allow-Origin" in security, (
        "DISTRIBUTION.md 的安全一节没提瓦片端口的 CORS 头 —— "
        "读者无从知道 5001 上的内容可被任意网页跨源读取")
    assert "跨源读取" in security or "跨源读" in security, (
        "只写了头名不够：必须点明它的后果是「任意网页可以跨源读取」")
    assert "GET" in security, (
        "必须同时写明边界：只有 GET、没有写入面，否则读者会高估风险")


def test_no_doc_pins_a_gdal_version_the_machine_must_choose():
    """安装类文档里 `GDAL==` 后面只能跟 `$(gdal-config --version)`，不能跟数字。

    `INSTALL.md` 曾写 `uv pip install --no-build-isolation GDAL==3.8.4`，而它自己
    的故障排除一节、`requirements.txt`、`README.md` 用的都是 `$(gdal-config
    --version)`。开发机 libgdal 是 3.11.4，照那行装等于拿 3.8.4 的 sdist 去对 3.11
    的头文件现编 —— 正是 `requirements.txt` 顶部整段注释在反对的操作，而
    `scripts/check_gdal.py` 只查范围，拦不住。

    上面 `test_no_doc_misquotes_the_requirements_gdal_spec` 抓不到这一条：那行
    压根没提 `requirements.txt`。
    """
    offenders = [
        f"{rel}:{lineno}: {line.strip()}"
        for rel in GDAL_DOCS
        for lineno, line in enumerate(_read(rel).splitlines(), 1)
        if re.search(r"GDAL\s*==\s*\d", line)
    ]
    assert not offenders, (
        "文档把 GDAL 钉成了一个具体版本 —— 绑定是 sdist 现编、版本跟随机器，"
        '唯一正确的写法是 "GDAL==$(gdal-config --version)"：\n  '
        + "\n  ".join(offenders))


def test_claude_md_states_the_real_tileparams_defaults():
    """`CLAUDE.md` 写的**应用侧**默认值必须与 `TileParams` 解析出来的一致。

    它曾写 `triangulator="auto"` / `normals=True`（那是 `build_terrain` 签名的默认，
    应用侧一步也走不到）。照它排障的人会以为产物带法线而实际不带 —— v0.2.13
    发版说明里那条「法线静默无效」正是这个失效形态。

    判据锚在**声称应用默认的那一行**上，不是全文搜字符串：全球底图那一节合法地
    写着 `triangulator="auto"`（底图确实用 auto，是有意分叉），全文搜会被它满足，
    把代码默认改回 auto 也照样绿。
    """
    defaults = _tile_params_defaults()
    claims = [l for l in _read(CLAUDE_MD).splitlines()
              if "application default is" in l]
    assert len(claims) == 1, (
        f"CLAUDE.md 里声称「应用侧默认」的行有 {len(claims)} 条，应当恰好 1 条")

    # 整句比对，不是「这一行里出现过这两个字符串」：同一行还合法地写着
    # `build_terrain` 签名默认 `normals=True`，散着搜会被它满足。
    expected = ('The application default is `triangulator="{}"` + `normals={}`'
                .format(defaults["triangulator"], defaults["normals"]))
    assert expected in claims[0], (
        f"CLAUDE.md 声称的应用默认与 TileParams 不一致，应为：{expected}\n"
        f"实际那一行：{claims[0].strip()[:200]}")


def test_claude_md_quotes_the_layer_json_extensions_expression_verbatim():
    """`extensions` 那一句必须**逐字**抄 `cesium_terrain` 里的条件表达式。

    旧版断言「瓦片带 oct 法线且 layer.json 声明 `extensions:
    ["octvertexnormals"]`」—— 漏掉了 `if normals else []` 这半句，而应用侧默认
    走的正是 `[]`。抄整条表达式，改哪一半都会红。
    """
    expr = '["octvertexnormals"] if normals else []'
    tiler = _read(os.path.join("src", "services", "terrain_tiling",
                               "cesium_terrain.py"))
    assert expr in tiler, (
        f"源码里已经没有 {expr!r} 这条表达式了 —— 先确认代码改成了什么，再改文档")
    assert expr in _read(CLAUDE_MD), (
        f"CLAUDE.md 没有逐字写出 layer.json 的 extensions 表达式 {expr!r}，"
        "读者会以为产物恒带法线")


def test_claude_md_names_the_preset_source_of_truth_and_both_gates():
    """三档预设：取值表的三个档名、唯一事实源、两个把关点、两个配置键都要在。

    `CLAUDE.md` 曾断言 `triangulator` / `max_error_k` / `normals`「不暴露给
    UI / DB / API，配置表、env、请求体、query string 都读不到」。照它写代码的人
    会认定改法线只能改代码或 CLI，于是绕开 `validate_tiling_quality` /
    `coerce_vertex_normals` —— 而这两个函数的 docstring 明写自己是唯一把关点，
    过了它们管理器只做 `bool()`，`bool('false')` 是 True。
    """
    text = _read(CLAUDE_MD)
    offsets = ast.literal_eval(_assigned(
        _module_ast(os.path.join("src", "services", "geo_validation.py")),
        "TILING_QUALITY_OFFSETS"))

    for preset in offsets:
        assert f"`{preset}`" in text, f"CLAUDE.md 没写出档位 {preset}"
    for symbol in ("TILING_QUALITY_OFFSETS", "validate_tiling_quality",
                   "coerce_vertex_normals", "effective_maxzoom"):
        assert symbol in text, f"CLAUDE.md 没指名 {symbol}"

    cfg = _default_configs()
    for key in ("terrain_quality_preset", "terrain_vertex_normals"):
        assert key in cfg, f"{key} 已经不在 DEFAULT_CONFIGS 里了"
        assert key in text, (
            f"CLAUDE.md 没写出配置键 {key} —— 上一版正是因此断言这些旋钮「不暴露」")
