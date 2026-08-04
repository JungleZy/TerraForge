"""MEDIUM #14 回归测试：apply_system_proxy 保留 bypass 列表（'no' → NO_PROXY），
'all' 键回退到 HTTP(S)_PROXY 而不是死写入 ALL_PROXY。"""
import os

import pytest

from src.services import system_proxy

_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """清掉真实环境里可能存在的代理变量，测试后由 monkeypatch 自动还原。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _fake_getproxies(monkeypatch, proxies):
    monkeypatch.setattr(system_proxy.urllib.request, "getproxies", lambda: proxies)


def test_no_bypass_exported_to_no_proxy(clean_proxy_env):
    """系统代理的 bypass 列表必须导出为 NO_PROXY，否则内网地址被错误走代理。"""
    _fake_getproxies(clean_proxy_env, {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
        "no": "localhost,127.0.0.1,192.168.0.0/16",
    })

    applied = system_proxy.apply_system_proxy()

    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,192.168.0.0/16"
    assert applied["NO_PROXY"] == "localhost,127.0.0.1,192.168.0.0/16"


def test_no_proxy_respects_existing_env(clean_proxy_env):
    """用户显式设置的 NO_PROXY 不被系统设置覆盖。"""
    clean_proxy_env.setenv("NO_PROXY", "internal.example.com")
    _fake_getproxies(clean_proxy_env, {
        "http": "http://127.0.0.1:7890",
        "no": "localhost,127.0.0.1",
    })

    applied = system_proxy.apply_system_proxy()

    assert os.environ["NO_PROXY"] == "internal.example.com"
    assert "NO_PROXY" not in applied


def test_all_key_falls_back_to_http_https(clean_proxy_env):
    """'all' 键（单代理全局生效）要落到 HTTP_PROXY/HTTPS_PROXY；
    aiohttp trust_env 不读 ALL_PROXY，绝不能写成 ALL_PROXY。"""
    _fake_getproxies(clean_proxy_env, {"all": "http://127.0.0.1:7890"})

    applied = system_proxy.apply_system_proxy()

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "ALL_PROXY" not in os.environ
    assert applied == {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }


def test_scheme_specific_key_wins_over_all(clean_proxy_env):
    """协议级键优先于 'all'：https 用自己的，http 缺失时回退 'all'。"""
    _fake_getproxies(clean_proxy_env, {
        "https": "http://127.0.0.1:7891",
        "all": "http://127.0.0.1:7890",
    })

    system_proxy.apply_system_proxy()

    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7891"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"


def test_existing_http_proxy_respected_with_all_fallback(clean_proxy_env):
    """环境变量已设置时尊重它，即使系统返回了 'all' 也不覆盖。"""
    clean_proxy_env.setenv("HTTP_PROXY", "http://user-set:8080")
    _fake_getproxies(clean_proxy_env, {"all": "http://127.0.0.1:7890"})

    applied = system_proxy.apply_system_proxy()

    assert os.environ["HTTP_PROXY"] == "http://user-set:8080"
    assert "HTTP_PROXY" not in applied
    # https 缺失，仍可从 'all' 回退
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_no_proxies_applies_nothing(clean_proxy_env):
    _fake_getproxies(clean_proxy_env, {})

    applied = system_proxy.apply_system_proxy()

    assert applied == {}
    for key in _ENV_KEYS:
        assert key not in os.environ


def test_getproxies_failure_returns_empty(clean_proxy_env):
    def _boom():
        raise RuntimeError("registry unavailable")

    clean_proxy_env.setattr(system_proxy.urllib.request, "getproxies", _boom)

    assert system_proxy.apply_system_proxy() == {}
    for key in _ENV_KEYS:
        assert key not in os.environ
