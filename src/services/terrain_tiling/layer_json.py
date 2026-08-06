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



def merge_base_availability(task_layer_path: Path, base_layer_path: Path) -> None:
    """把底图的 available 并进任务的 layer.json，并删掉 parentUrl。

    植入之后任务目录是自包含的，parentUrl 就成了多余的一次请求 —— 更糟的是它
    指向 localhost:5000，目录拷到别的机器上必然 404，而 Cesium 对这个 404 不
    报错，它塞一个假的 heightmap-1.0 图层并把 heightmapStructure 写在共享的
    builder 上，于是任务自己的 quantized-mesh 瓦片也按 heightmap 解析
    （v0.2.8 实测：4154 m 山峰解成 -744 m，控制台零报错）。

    maxzoom 取 max(底图, 任务)：maxzoom < 8 的退化任务里底图的 z6/z7 比任务更
    深，写任务的值会把这两层声明掉，Cesium 从此不请求它们 —— 文件在目录里却
    看不到。minzoom 同理硬置 0：ctb-tile 给小范围任务写的 minzoom 可能大于 0，
    留着就等于把刚植入的 z0-z7 全部作废。

    同层两边都有声明时取并集而不是二选一：磁盘上任务瓦片覆盖了底图的同名文件
    （植入是 skip-if-exists），但两边的 range 覆盖的 x/y 区间未必互相包含，丢
    掉任何一边都会漏声明真实存在的瓦片。range 去重只做逐字比较，不做区间归并
    —— Cesium 接受重叠声明，而重跑合成时的等值重复必须挡掉，否则声明会随重试
    次数线性膨胀。

    容忍 available / maxzoom 缺失或为 null（底图允许用户自备），但不容忍 JSON
    本身解不开：那时让异常抛给调用方回滚，静默吞掉只会产出一个没人知道是坏的
    layer.json。
    """
    task = json.loads(task_layer_path.read_text(encoding="utf-8"))
    base = json.loads(base_layer_path.read_text(encoding="utf-8"))

    task_av = task.get("available") or []
    base_av = base.get("available") or []
    merged = []
    for z in range(max(len(task_av), len(base_av))):
        ranges = list(base_av[z]) if z < len(base_av) else []
        for r in (task_av[z] if z < len(task_av) else []):
            if r not in ranges:
                ranges.append(r)
        merged.append(ranges)

    task["available"] = merged
    task["minzoom"] = 0
    task["maxzoom"] = max(int(base.get("maxzoom", 0) or 0),
                          int(task.get("maxzoom", 0) or 0))
    task.pop("parentUrl", None)

    task_layer_path.write_text(
        json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")