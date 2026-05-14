# Google Maps 下载器 - 可执行文件分发说明

本目录包含 Google Maps 下载器的独立可执行版本。

## 快速开始

### Windows
1. 解压 `map-downloader-windows.zip`
2. 双击 `map-downloader.exe`
3. 打开浏览器访问 `http://localhost:5000`

### macOS
1. 解压 `map-downloader-macos.tar.gz`
2. 打开终端，进入解压后的文件夹
3. 运行：`./map-downloader`
4. 打开浏览器访问 `http://localhost:5000`

### Linux
1. 解压 `map-downloader-linux.tar.gz`
2. 打开终端，进入解压后的文件夹
3. 运行：`./map-downloader`
4. 打开浏览器访问 `http://localhost:5000`

## 功能特性

- 无需安装 Python
- 无需安装依赖
- 可移植 - 可复制到任何机器运行
- 所有依赖已打包

## 系统要求

- **Windows**: Windows 10 或更高版本（64位）
- **macOS**: macOS 10.15 (Catalina) 或更高版本
- **Linux**: Ubuntu 20.04+ 或同等版本（64位）

## 目录结构

```
map-downloader/
├── map-downloader(.exe)    # 主程序
├── templates/              # Web 界面模板
├── static/                 # CSS、JS、图片
├── data/                   # 数据库（自动创建）
├── downloads/              # 下载的地图（自动创建）
└── cache/                  # 瓦片缓存（自动创建）
```

## 配置说明

应用首次运行时会自动创建必要的目录：
- `data/` - SQLite 数据库
- `downloads/` - 下载的地图文件
- `cache/` - 瓦片缓存以提高性能

## 故障排除

### 端口已被占用
如果端口 5000 已被占用，应用将无法启动。请关闭占用端口 5000 的其他应用。

### 防火墙警告
首次运行时，防火墙可能会询问权限。请允许应用接受传入连接。

### macOS 安全警告
如果 macOS 阻止应用运行：
1. 打开"系统偏好设置" → "安全性与隐私"
2. 点击"仍要打开" map-downloader

### Linux 权限错误
如果遇到权限错误：
```bash
chmod +x map-downloader
```

## 从源码构建

如果你想自己构建可执行文件：

1. 安装 Python 3.9+
2. 安装依赖：`pip install -r requirements.txt`
3. 安装 PyInstaller：`pip install pyinstaller`
4. 运行：`pyinstaller build.spec`
5. 可执行文件位于 `dist/map-downloader/`

## 技术支持

如有问题，请访问：
https://github.com/YOUR_USERNAME/map-download/issues

## 免责声明

本工具仅供学习和研究使用。使用者应遵守 Google Maps 服务条款和相关法律法规。作者不对使用本工具产生的任何后果负责。
