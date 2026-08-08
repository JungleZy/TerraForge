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


_DOC_ENDPOINT = re.compile(r"^-\s+`([A-Z|]+)\s+(/\S*?)`")


def _collect_readme_routes():
    """README「## API 端点」一节里的端点条目，返回 {(method, 归一化路径)}。"""
    text = _read(README)
    start = text.index("\n## API 端点\n")
    rest = text[start + 1:]
    end = rest.index("\n## ", 1)
    section = rest[:end]

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

    旧的 `INSTALL.md:120` 让读者去改 `requirements.txt` 里的 `GDAL==3.8.4`——那行
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
    text = _read(README)
    start = text.index("\n## 项目结构\n")
    section = text[start:text.index("\n## ", start + 1)]
    return section.split("```")[1]


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
            if not os.path.exists(os.path.join(PROJECT_ROOT, rel)):
                missing.append(f"{rel!r}（来自 {entry!r}）")
    assert not missing, "README 项目结构树里不存在的条目：\n  " + "\n  ".join(missing)


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
