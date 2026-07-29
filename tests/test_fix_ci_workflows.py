"""C7 修复契约测试 —— CI workflow 必须在打包前真正执行 pytest。

文本级断言（项目无 PyYAML 依赖，且 tests/ 已有文本契约测试先例）：
- build.yml 与 test-build.yml 都必须包含 `python -m pytest tests/` 步骤；
- pytest 步骤必须出现在 `python nuitka_build.py` 步骤之前，测试失败则构建失败。
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = [
    os.path.join(ROOT, '.github', 'workflows', 'build.yml'),
    os.path.join(ROOT, '.github', 'workflows', 'test-build.yml'),
]


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


@pytest.mark.parametrize('path', WORKFLOWS, ids=[os.path.basename(p) for p in WORKFLOWS])
def test_workflow_runs_pytest(path):
    content = _read(path)
    assert 'python -m pytest tests/' in content, (
        f'{os.path.basename(path)} 从不执行测试套件（C7）'
    )


@pytest.mark.parametrize('path', WORKFLOWS, ids=[os.path.basename(p) for p in WORKFLOWS])
def test_pytest_runs_before_nuitka(path):
    content = _read(path)
    pytest_pos = content.find('python -m pytest tests/')
    build_pos = content.find('python nuitka_build.py')
    assert pytest_pos != -1, '缺少 pytest 步骤'
    assert build_pos != -1, '缺少 Nuitka 打包步骤'
    assert pytest_pos < build_pos, (
        'pytest 必须在 Nuitka 打包之前运行，测试失败应使构建失败'
    )
