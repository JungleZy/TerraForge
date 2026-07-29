# 快速启动指南

## 前置条件

确保你的系统已安装：
- Python 3.8+
- GDAL 库

## 一键启动

### 1. 安装 GDAL（如果尚未安装）

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3-gdal
```

**macOS:**
```bash
brew install gdal
```

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 3. 初始化数据库

```bash
python database.py
```

预期输出：`Database initialized successfully`

### 4. 启动应用

```bash
python app.py
```

预期输出：
```
Database initialized successfully
 * Running on http://0.0.0.0:5000
```

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
   - **输出格式**: 选择 "瓦片+拼接图"
   - **保存路径**: 保持默认 `./downloads/map`
4. 点击 "创建下载任务"
5. 在右侧任务列表中点击 "启动" 按钮
6. 观察实时进度更新

### 查看下载结果

下载完成后，文件保存在：
- **瓦片**: `cache/<style>/<zoom>/<x>/<y>.png`
- **拼接图**: `downloads/map_z<zoom>.tif`

### 查看历史记录

访问 `http://localhost:5000/history` 查看所有下载任务的历史记录和地图可视化。

### 修改配置

访问 `http://localhost:5000/config` 修改系统配置，例如：
- 增加并发下载数（提高速度）
- 启用/禁用缓存
- 修改默认保存路径

## 常见问题

### Q: 启动时提示 "ModuleNotFoundError: No module named 'flask'"
**A:** 运行 `pip install -r requirements.txt` 安装依赖

### Q: 启动时提示 "ImportError: No module named 'osgeo'"
**A:** 安装 GDAL 系统库和 Python 绑定

### Q: 下载速度很慢
**A:** 
- 在配置页面增加并发下载数（默认 10，可增加到 20-50）
- 检查网络连接
- 考虑使用代理服务器

### Q: 任务一直显示 "运行中" 但没有进度
**A:** 
- 检查浏览器控制台是否有错误
- 确认 WebSocket 连接正常（应该看到 "Connected to server"）
- 重启应用

### Q: 下载的文件在哪里？
**A:** 
- 原始瓦片：`cache/<style>/<zoom>/<x>/<y>.png`
- 拼接图像：`downloads/` 目录下的 `.tif` 文件

## 注意事项

⚠️ **重要提示**：
- 首次使用建议选择小区域和低缩放级别（10-12）进行测试
- 大区域高缩放级别可能产生数万甚至数百万个瓦片，需要数小时下载
- 确保有足够的磁盘空间
- 遵守 Google Maps 服务条款，仅用于个人学习研究

## 下一步

- 阅读完整的 [README.md](README.md) 了解更多功能
- 查看 [API 文档](README.md#api-端点) 了解如何通过 API 使用
- 探索配置页面的高级选项

## 技术支持

如遇到问题，请检查：
1. Python 版本是否 >= 3.8
2. GDAL 是否正确安装
3. 所有依赖是否已安装
4. 数据库是否已初始化
5. 端口 5000 是否被占用

祝使用愉快！🎉
