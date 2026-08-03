# docs/guides —— 照着做的操作指南

四份文档，覆盖从「装环境」到「把成品交给别人」的全链路。**它们描述当前实现，照着做能跑通**——这是 docs/ 下唯一可以直接执行的一层（其余目录见 [`../README.md`](../README.md) 的可信度说明）。

## 四份的分工

| 文档 | 读者 | 什么时候读 |
|---|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | 环境已装好的人 | 想尽快启动应用、框选一小块区域跑完第一个下载任务，顺带知道产物落在哪、配置页能改什么 |
| [`INSTALL.md`](INSTALL.md) | 从零搭开发环境的人 | 装 Python 3.12 + uv + GDAL 系统库 + GDAL Python 绑定，含分平台路线（Linux 走 apt+uv 编译，Windows / Apple Silicon Mac 走 conda-forge）与故障排除 |
| [`BUILD.md`](BUILD.md) | 要出可执行文件的人 | Nuitka 本地打包、GitHub Actions 矩阵构建、发版前检查清单、CI 覆盖范围的边界 |
| [`DISTRIBUTION.md`](DISTRIBUTION.md) | **最终用户**（拿到 zip / tar.gz 的人，不是开发者） | 解压怎么运行、目录不能拆开搬、防火墙提示该不该点允许、macOS 隔离属性怎么清 |

## 阅读顺序

- **第一次接触本项目**：`INSTALL.md` → `QUICKSTART.md`。反过来读会卡在 GDAL 上。
- **只是要发个版**：`BUILD.md` 的「发版前检查」四条先过一遍，再看构建命令。
- **不写代码、只想用**：只读 `DISTRIBUTION.md`，其余三份都不需要。别把 `INSTALL.md` 甩给最终用户——那是源码安装流程，用不上。

## 一条必须先知道的坑：GDAL 装的顺序

`INSTALL.md` 和 `BUILD.md` 都要装 GDAL，**两处的命令顺序都不能调换**：先 `setuptools wheel` → 再 `numpy` → 最后 `--no-build-isolation` 装 GDAL。

原因：GDAL 的 Python 绑定在 PyPI 上只有源码包，装的时候现场编译，而 `_gdal_array` 这个 C 扩展**只有在编译当时能 `import numpy` 才会被编出来**。默认的 build isolation 会把 GDAL 丢进一个没有 numpy 的临时干净环境里编译，于是 `_gdal_array` 被静默跳过，安装照样报「成功」。

为什么它是本项目最难发现的坑：

- `import gdal` 照常成功，应用能启动，瓦片能下载 —— 表面上一切正常；
- **打包不会报错，CI 冒烟测试也照样绿**（冒烟只请求首页，完全不碰 GDAL 代码路径），坏包能一路发到用户手里；
- 只有真正读写像素的环节才炸：瓦片拼接 GeoTIFF、地形切片、等高线渲染，报 `ImportError: cannot import name '_gdal_array' from 'osgeo'`。

所以装完必须验证这一条（只验 `import gdal` 查不出问题）：

```bash
uv run python -c "from osgeo import gdal_array; print(gdal_array.__file__)"
```

已经装坏了怎么重建，见 [`INSTALL.md` 的故障排除](INSTALL.md#importerror-cannot-import-name-_gdal_array-from-osgeo)——注意重建时 `UV_NO_CACHE=1` 不能省，否则 uv 会静默复用之前那个没有 numpy 的构建缓存，白重装一遍。

> 走 conda-forge 路线（Windows / Apple Silicon Mac）不会遇到这个问题：conda 的 gdal 包自带已含 numpy 支持的预编译绑定，不需要编译。但这条路线下所有 `uv run python xxx` 要换成直接 `python xxx`。
