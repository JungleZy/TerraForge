# 源码目录整理：四个顶级包移入 src/ 设计

日期：2026-08-04
状态：待实施（本文只定方案，未动代码）

## 背景

根目录平铺着 `core/`、`models/`、`routes/`、`services/` 四个顶级包
（43 文件 13405 行），与 `templates/`、`static/`、`docs/`、`scripts/`、
`tests/` 以及各类配置文件混在一层。目标是把 Python 源码集中到 `src/` 下，
根目录只保留应用入口。

## 需求

1. 四个顶级包移入 `src/`。
2. 根目录 Python 文件只留 `app.py`（入口）与 `nuitka_build.py`（构建驱动）。
3. 不引入新的构建/安装步骤，不动现有 Nuitka 打包链与 CI。

## 设计

### 选型：`src/` 自身即包，不用标准 src 布局

`src/__init__.py` 存在，`src` 成为可导入包，导入写成
`from src.core.config import Config`。

这样做的唯一理由是**零机制成本**：`app.py` 在根目录 → 根目录天然在
`sys.path[0]` → `import src.core.bundle` 直接解析。Nuitka 的静态分析顺着走
（与今天顺着 `core.bundle` 同构）、pytest 的 `sys.path.insert(root)` 不用改、
CI 的 `python -m pytest tests/` 与 `python nuitka_build.py` 一个字不用改。

两条被否决的替代方案，理由都在 Nuitka：

- **`src/` 仅作目录、import 不变**（靠 `PYTHONPATH=src`）：Nuitka 不执行
  `sys.path.insert`，只做静态分析，整条打包链要押在本仓库从未验证过的
  `PYTHONPATH` 机制上。且省下的 493 处 import 会以 105 处
  `sys.path.insert(root)` → `insert(root/src)` 的形式还回来，diff 并没小多少。
- **标准 src 布局 `src/terraforge/` + `pyproject.toml` + 可编辑安装**：
  PEP 660 可编辑安装装的是 `__editable___*_finder` 导入钩子，Nuitka 静态分析
  跟不住，需退回 `editable_mode=compat` 之类的绕法；还要把
  `uv pip install -e .` 加进 build.sh / build.bat / 3 个 CI job。给一条已经
  稳定的打包链换地基，收益不抵风险。

`src` 作为包名偏泛（`from src.services.task_manager import TaskManager`），
改叫 `terraforge/` 可消除此缺点且技术实现完全相同 —— 已决策沿用 `src`。

### 目标结构

```
map-download/
├── app.py                  # 根目录唯一应用文件，模块名保持 `app`
├── nuitka_build.py         # 构建驱动，留在根目录（理由见下）
├── src/
│   ├── __init__.py         # 新建，空文件
│   ├── core/  models/  routes/  services/
├── tests/                  # 不动
├── templates/  static/     # 不动，非 Python 代码
└── docs/  scripts/  .github/  requirements.txt  build.sh  build.bat
```

`app.py` 保持根目录且模块名保持 `app` 是刻意的：`gunicorn app:app`、
108 个测试的 `import app`、conftest 的 `fresh_import(monkeypatch, "app", ...)`、
Nuitka 的 `ENTRY='app.py'` 与输出目录名 `dist/app.dist` 全部零改动。

`nuitka_build.py` 留在根目录：它与 `app.py` 同为入口而非库代码；移进
`scripts/` 要改 build.sh、build.bat、两个 workflow 里 3 处调用，以及 **2 处
`hashFiles('requirements.txt','nuitka_build.py')` 缓存 key**（漏改会让
Nuitka 编译缓存彻底失效，回到每次发版全量编译约 41 分钟，
即 ea94a8ab4 / 437db9ae4 两个 commit 刚修好的问题），而它内部全用相对路径
（`dist`、`templates`、`ENTRY='app.py'`），移过去也必须仍从根目录调用 ——
收益零，踩坑面积不小。

## 改动清单

| 组 | 内容 | 量 |
|---|---|---|
| 1 | `git mv` 四个包进 `src/`；新建 `src/__init__.py`；清 `__pycache__` | 6 条命令 |
| 2 | import 语句：`^(\s*)(from\|import)\s+(core\|models\|routes\|services)\b` 前缀 `src.` | **493 处** |
| 3 | 字符串模块名：`['"](core\|models\|routes\|services)(\.\w+)+['"]` 前缀 `src.` | **302 处** |
| 4 | `src/core/config.py:58` `BASE_DIR` 层级 +1（见「静默失败点 ①」） | **1 处，唯一语义改动** |
| 5 | 脚本与元测试的路径字面量 | 约 6 处 |
| 6 | 文档路径 | 42 份 md，建议只改 4 类 |

第 2 组必须覆盖 `src/*/__init__.py` —— `routes/__init__.py` 用的是绝对导入
`from routes.main import main_bp`（models、services 同理），漏了就 import
不到自己。

第 3 组是 sed 改 import 抓不到的那批，集中在 `tests/conftest.py` 的
`fresh_import(...)` 与 `_INJECTED_MANAGER_GLOBALS`、各测试的
`monkeypatch.setattr("services.x.y", ...)`。

第 5 组具体位置：`scripts/push-release.sh:12`、`scripts/push-release.bat:12`
（grep `core/config.py` 取版本号）、`tests/test_fix_build_scripts.py:96`、
`tests/test_fix_l1_entry_build_misc.py:94,130`。

第 6 组只改 `CLAUDE.md`、`README.md`、`docs/guides/`、`docs/reference/`。
`docs/archive/`、`docs/reviews/`、`docs/superpowers/` 是历史快照，改了反而
失真 —— 在 `docs/README.md` 加一句「0.2.7 起源码移入 `src/`，历史文档中的
路径按旧布局阅读」即可。

**明确不改**：`nuitka_build.py` 的全部 Nuitka 参数、两个 CI workflow、
build.sh / build.bat、.gitignore、.gitattributes、requirements.txt、
tests/ 里 105 处 `sys.path.insert(root)`。

## 静默失败点（本方案的风险全在这四条）

第 2、3 组漏改会当场抛 `ModuleNotFoundError`（`importlib.import_module` 与
`monkeypatch.setattr` 都是 raising 的），属安全失败。真正危险的是下面四条
——**不报错，但行为已经错了**：

**① `Config.BASE_DIR` 少数一级**（`src/core/config.py:58`）—— 最危险。
`Path(__file__).parent.parent` 需改成 `.parent.parent.parent`。忘了改，
`BASE_DIR` 变成 `<root>/src`，程序照常启动，然后在 `src/data/` 下建一个
**全新的空数据库**，`src/downloads/`、`src/cache/` 跟着新建。表现是「所有
历史任务和配置凭空消失」，不抛任何异常。
> 验证：`uv run python -c "from src.core.config import Config; print(Config.BASE_DIR)"`
> 必须打印项目根目录。

**② `tests/conftest.py:65-69` 的 `/tmp` 沙箱 fixture** —— `import
services.task_cleanup as tc` 包在 `try/except Exception: return` 里。名字漏改
就**静默跳过**该 fixture，测试套件重新开始 rmtree 本机真实的
`/tmp/map_dl_stitch_*`、`/tmp/contour_warp_*`（该 fixture 的 docstring 记录
了这是复现过的 flaky）。测试全绿，但每跑一次测试都可能炸掉正在跑的 GDAL 任务。
> 验证：手建 `/tmp/map_dl_stitch_PROOF`，跑一个静态路由测试，该目录必须仍在。

**③ `tests/conftest.py:92-95` 的 `_preserve_injected_globals`** ——
用 `sys.modules.get(mod_name)`，取不到静默跳过。`_INJECTED_MANAGER_GLOBALS`
里 5 个元组（`("routes.api","task_manager")` 等）漏改，注释描述的 M23
「测试 patch 新模块、请求却打到旧模块」跨用例污染就悄悄回来，表现为测试结果
依赖执行顺序。

**④ 陈旧 `__pycache__`** —— `git mv` 会把 `core/__pycache__/` 一起搬走，
根目录另有一个 `__pycache__/`。清一遍 5 秒，不清就是排查两小时的诡异
ImportError。

## 实施与验证顺序

三个 commit，每个都能独立跑通全套测试：

1. **commit 1** —— 第 1~4 组。门槛：`uv run pytest tests/ -q` 全绿
   **且** `Config.BASE_DIR` 输出根目录 **且** 静默失败点 ② 的 `/tmp` 验证通过。
2. **commit 2** —— 第 5 组。门槛：`scripts/push-release.sh` 能取到版本号。
3. **commit 3** —— 第 6 组文档。

**打包验证是硬门槛，测试套件替代不了**：本地跑一次 `./build.sh`，然后
`cd dist/terraforge && ./terraforge` 请求首页拿到 200。模块没打进去、GDAL
挂了这类问题测试一个都测不出来 —— CI 里那步 smoke test 存在就是因为这个。
这一步过了再推 master。
