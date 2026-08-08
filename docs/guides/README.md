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

先 `setuptools wheel` → 再 `numpy` → 最后 `--no-build-isolation` 装 GDAL。顺序反了会**静默**装出一个缺 `_gdal_array` 的绑定：`import gdal` 成功、应用能起、瓦片能下、打包不报错、CI 冒烟也照样绿，只有真正读写像素的环节（拼接 GeoTIFF、地形切片、等高线渲染）才炸——这是本项目最难发现的坑。

**这个话题只有一个主人：[`INSTALL.md`](INSTALL.md)。** 分平台路线、验证命令、以及「已经装坏了怎么重建」都在那里，本文不复述命令——同一串命令抄在多处，就会各自漂移。
