# 并发下载数「测速推荐」设计

日期：2026-08-01
状态：已实现（随本轮改动落地）

## 背景

第一轮性能修复把 `limit_per_host` 改为跟随 `concurrent_downloads`、默认并发
10 → 50 之后，并发数成为吞吐的主要旋钮，但它的合理值完全取决于运行环境
（代理质量、运营商、对端限速），用户没有手段知道该填多少。

## 需求

1. 并发下载数保持可配置（配置页既有字段，不动）。
2. 字段旁边加「测速推荐」按钮：按当前电脑环境实测，给出推荐值并填入输入框
   （不自动保存，沿用配置页「保存配置」的既有语义）。

## 方案选型

- **A. CPU 公式**（核数 × 系数）：否决。瓦片下载是纯 IO 等待，核数与吞吐
  无关，真正的变量是链路延迟和代理质量。
- **B. 延迟探测 + Little 定律**：轻量，但目标吞吐是拍脑袋常数，链路饱和点
  测不出来。
- **C. 实测吞吐阶梯（采用）**：用已保存的 tile_servers / proxy_url 做真实
  瓦片下载测速，逐级（10 / 25 / 50）在固定时间窗（8s）内按该档并发下载，
  计完成数得吞吐；取膝点（达到最高吞吐 90% 的最小并发）。顶格仍上升
  ≥15% 时标记 rising，提示可再手动调高。CPU 不参与。

## 组件

- `services/tile_url_probe.py`
  - `_measure_throughput(urls, concurrency, proxy_url, window_s, fetch=None)`：
    worker 池 + 时间窗，窗口结束取消在途请求只计完成数；默认 fetch 建
    连接池 session（limit/limit_per_host=concurrency、trust_env，与下载
    引擎同款），`fetch` 可注入（测试无网可测）。
  - `_pick_concurrency(samples)`：纯函数，膝点 + rising 判定。
  - `recommend_concurrency(servers, style, proxy_url, center_lng/lat,
    measure=None)`：sync 包装（内部 asyncio.run，同 probe_server_entry）；
    一切故障归一成 fallback（保守值 20），不抛给路由。
- `routes/api.py`：`POST /api/config/recommend_concurrency`，读配置后调
  上者；额外兜一层异常，始终 200 + fallback。
- `templates/_config_content.html`：并发字段改 input-group + 「测速推荐」
  按钮 + hint 小字。
- `static/js/config.js`：`initConcurrencyRecommend()`，点击 → 禁用按钮 +
  提示 → fetch → 填入数值 + 显示实测摘要；不落库。

## 护栏

- 推荐值 clamp 到配置校验域 1–100，填入即可保存。
- 测速瓦片只读不写 cache；URL 以地图中心 z12 网格铺开、服务器逐条轮换；
  内网/回环地址复用 should_bypass_proxy 不带代理。
- 探测全失败 / 流程异常 → `{recommended: 20, fallback: true}`，前端原样
  展示 note。

## 测试

`tests/test_concurrency_recommend.py`：膝点三种形态 + 空样本；编排层用
stub measure 覆盖推荐/rising/fallback/clamp；`_measure_throughput` 用假
fetch 在真实事件循环上验证完成计数、时间窗取消、失败不计数；API 端点
monkeypatch 服务函数；模板/JS 接线按项目惯例做源码断言。
