import json
from pathlib import Path


def normalize_parent_url(parent_url: str | None) -> str | None:
    """把 parentUrl 规整成**目录**形式；空值返回 None（= 不写该字段）。

    Cesium 对 parentUrl 做 appendForwardSlash() 之后再拼 layer.json，所以这里
    必须给目录。给 `.../base/layer.json` 会让它去请求
    `.../base/layer.json/layer.json` —— 404。

    而 Cesium 的 404 处理不抛错：它塞一个假的 heightmap-1.0 图层，并把
    heightmapStructure 写在**共享的 builder** 上，于是 requestTileGeometry 对
    **本任务自己的 quantized-mesh 瓦片**也走 heightmap 分支去解析。

    实测（天山 N42E086，同一批瓦片只改 parentUrl，同源 localhost，2026-08-05）：

        parentUrl                     高程 (86.5,42.5)/(86.2,42.2)/(86.8,42.8)
        .../terrain/base/layer.json   -859.1 / -956.4 / -743.7      <- 旧默认值
        .../terrain/base              2656.6 / 1092.3 / 4154.2      <- 正确
        无 parentUrl                  2656.6 / 1092.3 / 4154.2      <- 正确

    源 DEM 真值 2672 / 1086 / 4154：**4154 m 的山峰被解成海平面以下 744 m**，
    而 provider.hasVertexNormals 仍报 true、瓦片全 200、控制台一条错都没有。
    两条触发路径在生产上都恒真 —— base 存在则多拼一层 404，base 不存在则本身 404。

    规整放在这个唯一写入点、而不是只改 DEFAULT_CONFIGS：改默认值只对新建的库
    生效，**存量 config 表里那一行仍是坏的**，用户不会去改它。
    """
    if not parent_url:
        return None
    url = parent_url.strip().rstrip("/")
    if url.lower().endswith("/layer.json"):
        url = url[: -len("/layer.json")].rstrip("/")
    return url or None


def parent_url_if_base_available(parent_url: str | None,
                                 base_dir: Path | None) -> str | None:
    """base 地形不可达时返回 None（= 不写 parentUrl）；可达时返回规整后的 URL。

    **写一个指向 404 的 parentUrl，比根本不写更糟。** Cesium 拿不到 parentUrl
    指向的 layer.json 时不报错，而是塞一个假的 heightmap-1.0 图层，并把
    heightmapStructure 写在**共享的 builder** 上 —— 于是本任务自己的
    quantized-mesh 瓦片也按 heightmap 解析。

    这条闸门补的是 normalize_parent_url 漏掉的另一半：URL 格式只是两个触发
    条件之一，根因是「指向不可达资源」。全球 base 是**可选**产物
    （docs/reference/terrain/global-base-build.md，需自备几 GB 到上百 GB 的全球
    DEM），所以「没建 base」才是默认装机的常态。

    实测（base_z8 不存在，parentUrl 已是正确的目录形式 .../terrain/base）：

        末层 isHeightmap  true          瓦片类型      HeightmapTerrainData
        heightmapStructure 有值         法线          无
        高程              -859.1 / -956.4 / -743.7   （真值 2672 / 1086 / 4154）

    去掉 parentUrl 后同一批瓦片：高程 2656.6 / 1092.3 / 4154.2、法线可用。

    判据用**本地目录里有没有 layer.json**，不去请求那个 URL：切片是服务端行为，
    此刻 Flask 未必起着（默认 URL 就指向 localhost:5000），网络探测既慢又会
    引入不确定性；而 base 是本机磁盘上的产物，存在性是可靠的本地事实。
    """
    if base_dir is None:
        return None
    try:
        if not (Path(base_dir) / "layer.json").is_file():
            return None
    except OSError:
        return None
    return normalize_parent_url(parent_url)


def patch_layer_json_parent(layer_json_path: Path, parent_url: str | None) -> None:
    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    normalized = normalize_parent_url(parent_url)
    if normalized is None:
        data.pop("parentUrl", None)
    else:
        data["parentUrl"] = normalized
    layer_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
