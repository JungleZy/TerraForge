# 保存路径绝对化 + 「浏览」目录选择设计

日期：2026-08-02
状态：已实现

## 背景

保存路径此前允许相对值（`./downloads/map`）：相对路径历史上按进程 CWD 解析，
打包 exe 换目录启动后落盘位置漂移；输入框只能手敲，用户得自己猜绝对路径。

## 需求

1. 保存路径一律绝对路径（任务表单的 outputPath、配置页 default_save_path）。
2. 路径可点按钮选择，也可手动输入。

## 设计

### 边界不变

任务产物必须落在 `Config.DOWNLOADS_DIR` 之内（既有安全模型，
remove_task_dir_if_safe / create_task 都按它守）。绝对化不改变边界，
只改变输入形态；「浏览」也只浏览该根目录之内。

### 后端

- `geo_validation.require_absolute_output_dir`：与 resolve_output_dir 同边界，
  相对路径直接 ValueError（文案指路「浏览」按钮）。resolve_output_dir 保留，
  仅用于读存量历史任务行（可能是相对值）。
- 两条建任务路径（map / DEM）的 create_task 改用它；contour/local_terrain
  是上传驱动、不读该字段，不动。
- `ConfigManager.validate_config` 新增 default_save_path 分支：相对/越界拒绝
  （存一个建任务必 400 的值没有意义）。
- `init_database` 一次性归一：存量相对 default_save_path 按历史语义
  （相对 BASE_DIR，不是 DOWNLOADS_DIR —— './downloads' 就是根目录本身）
  转绝对；越界保留原值只警告。
- `GET /api/fs/browse?path=`：弹窗数据源。只列根目录内非隐藏子目录；
  越界/不存在/非目录 400；parent=null 表示到根。

### 前端

- `templates/_path_browser_modal.html` + `static/js/path_browser.js`：
  Bootstrap modal，上一级/子目录导航，「选择此目录」写回 input 并派发
  input 事件（map.js 的 userEdited 标记靠它，类型切换不覆盖用户选择）。
  base.html 全站加载，首页任务表单与配置页共用。
- 任务表单默认值：`config.default_save_path`（已是绝对值）+ `/map` 或
  `/dem`，不再硬编码相对值。
- 「浏览」按钮用与「验证」同款的紧凑配方（`.btn.path-browse`）。
- `.list-group-item` 背景规则盖掉 Bootstrap 默认灰（CSS 契约测试要求）。

## 测试

- `tests/test_path_browser.py`：require_absolute 四态、建任务/配置保存的
  相对拒绝、init 归一、浏览 API 四态、前端接线。
- 旧口径测试批量翻面（相对 → 绝对），含
  test_fix_dem_output_path.py 钉旧行为的用例改为钉拒绝。
- 浏览器实测：表单默认绝对、弹窗导航/写回/userEdited、配置页按钮。
