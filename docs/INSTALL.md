# 安装指南

## 当前状态

✅ **已完成**: GDAL 已安装  
❌ **缺少**: pip 和 Python 依赖包

## 需要手动执行的安装步骤

由于需要 sudo 权限，请在终端中手动执行以下命令：

### 1. 安装 pip 和 venv

```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv
```

### 2. 创建虚拟环境（推荐）

```bash
cd /home/jungle/workspace/map-download
python3 -m venv venv --system-site-packages
source venv/bin/activate
```

### 3. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

**注意**: 如果 GDAL 安装失败，可以跳过它（因为系统已经安装了 python3-gdal）：

```bash
pip install Flask==2.3.3 Flask-SocketIO==5.3.4 python-socketio==5.9.0 python-engineio==4.7.1 aiohttp==3.9.1 aiofiles==23.2.1 Pillow==10.1.0 pytest==7.4.3
```

### 4. 验证安装

```bash
python3 -c "import flask; print('Flask:', flask.__version__)"
python3 -c "from osgeo import gdal; print('GDAL:', gdal.__version__)"
```

### 5. 启动应用

```bash
python3 app.py
```

应用将在 `http://0.0.0.0:5000` 启动。

## 替代方案：使用系统包（不推荐）

如果不想使用虚拟环境，可以直接安装系统包：

```bash
sudo apt-get install -y python3-flask python3-aiohttp python3-pil
pip3 install --user Flask-SocketIO==5.3.4 python-socketio==5.9.0 python-engineio==4.7.1 aiofiles==23.2.1
```

## 故障排除

### 问题：pip3 命令未找到
**解决**: 运行 `sudo apt-get install -y python3-pip`

### 问题：GDAL 版本不匹配
**解决**: 
```bash
# 检查系统 GDAL 版本
gdal-config --version

# 安装匹配的 Python GDAL 绑定
pip install GDAL==$(gdal-config --version)
```

### 问题：权限错误
**解决**: 使用虚拟环境或添加 `--user` 参数：
```bash
pip install --user -r requirements.txt
```

## 快速一键安装（需要 sudo）

如果你有 sudo 权限，可以运行：

```bash
sudo apt-get update && \
sudo apt-get install -y python3-pip python3-venv && \
cd /home/jungle/workspace/map-download && \
python3 -m venv venv --system-site-packages && \
source venv/bin/activate && \
pip install Flask==2.3.3 Flask-SocketIO==5.3.4 python-socketio==5.9.0 python-engineio==4.7.1 aiohttp==3.9.1 aiofiles==23.2.1 Pillow==10.1.0 pytest==7.4.3 && \
python3 app.py
```
