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
