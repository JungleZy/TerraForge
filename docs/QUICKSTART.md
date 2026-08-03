# 快速启动指南

## 前置条件

确保你的系统已安装：
- Python 3.12+
- GDAL 系统库
- [uv](https://docs.astral.sh/uv/)（本项目用它管理虚拟环境与依赖）

## 一键启动

### 1. 安装 GDAL（如果尚未安装）

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev
```

**macOS:**
```bash
brew install gdal
```

### 2. 安装 Python 依赖

```bash
uv venv                              # 如果 .venv 不存在
uv pip install -r requirements.txt
```

如果 GDAL Python 绑定安装失败，改用与系统 GDAL 匹配的版本：

```bash
uv pip install gdal==$(gdal-config --version)
```

### 3. 初始化数据库

```bash
uv run python -c "from core.database import init_database; init_database()"
```

> 启动应用时也会自动执行（幂等），此步骤可跳过。

### 4. 启动应用

```bash
uv run python app.py
```

应用将在 `http://0.0.0.0:5000` 启动。

### 5. 访问应用

在浏览器中打开：`http://localhost:5000`

## 快速使用

### 创建第一个下载任务

1. 在主页地图上，点击矩形工具（□）
2. 在地图上拖动鼠标框选一个小区域（建议先测试小区域）
3. 填写任务参数：
   - **任务名称**: 例如 "测试下载"
   - **地图样式**: 选择 "标准地图"
   - **最小缩放级别**: 10
   - **最大缩放级别**: 12（建议先用小范围测试）
   - **输出格式**: 保持「瓦片」「GeoTIFF」两个复选框都勾选（瓦片+拼接图）
   - **保存路径**: 默认为配置的默认保存路径下的 `map/` 子目录，必须是绝对路径；可点击「浏览」在弹窗中选择
4. 点击 "创建下载任务"
5. 在右侧任务列表中点击 "启动" 按钮
6. 观察实时进度更新（下载到 100% 后还会经历拼接/复制阶段，任务行会显示对应进度）

### 查看下载结果

下载完成后，文件保存在任务产物目录 `<保存路径>/task_<任务ID>/` 下：

- **瓦片**: `<保存路径>/task_<id>/<zoom>/<x>/<y>.png`（下载过程中实时镜像；全局共享缓存另存于 `cache/<style>/<zoom>/<x>/<y>.png`）
- **拼接图**: `<保存路径>/task_<id>/<任务名>_zoom_<zoom>.tif`

### 查看历史记录

访问 `http://localhost:5000/history` 查看所有下载任务的历史记录和地图可视化。

### 修改配置

访问 `http://localhost:5000/config` 修改系统配置，例如：
- 调整并发下载数（可用「测速推荐」按当前网络实测推荐值）
- 启用/禁用缓存，或在「缓存管理」按分类查看占用并手动清理
- 修改默认保存路径（需绝对路径）

## 常见问题

### Q: 启动时提示 "ModuleNotFoundError: No module named 'flask'"
**A:** 运行 `uv pip install -r requirements.txt` 安装依赖

### Q: 启动时提示 "ImportError: No module named 'osgeo'"
**A:** 安装 GDAL 系统库和 Python 绑定，详见 [INSTALL.md](INSTALL.md)

### Q: 下载速度很慢
**A:**
- 在配置页面使用「测速推荐」或手动增加并发下载数
- 检查网络连接
- 考虑使用代理服务器

### Q: 任务一直显示 "运行中" 但没有进度
**A:**
- 检查浏览器控制台是否有错误
- 确认 WebSocket 连接正常（应该看到 "Connected to server"）
- 重启应用

### Q: 下载的文件在哪里？
**A:**
- 任务产物（瓦片 + 拼接图）：`<保存路径>/task_<任务ID>/`
- 全局瓦片缓存：`cache/<style>/<zoom>/<x>/<y>.png`

## 注意事项

⚠️ **重要提示**：
- 首次使用建议选择小区域和低缩放级别（10-12）进行测试
- 大区域高缩放级别可能产生数万甚至数百万个瓦片，需要数小时下载
- 确保有足够的磁盘空间
- 遵守 Google Maps 服务条款，仅用于个人学习研究

## 下一步

- 阅读完整的 [README.md](../README.md) 了解更多功能
- 查看 [API 文档](../README.md#api-端点) 了解如何通过 API 使用
- 探索配置页面的高级选项

## 技术支持

如遇到问题，请检查：
1. Python 版本是否 >= 3.12
2. GDAL 是否正确安装（`gdal-config --version`）
3. 所有依赖是否已安装
4. 端口 5000 是否被占用

祝使用愉快！🎉
