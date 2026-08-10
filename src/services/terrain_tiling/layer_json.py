import json
from pathlib import Path
from urllib.parse import urlsplit

# 「应用自己的旧地址」的两种写法。启动横幅印 localhost，浏览器地址栏和手改
# config 的人写 127.0.0.1，指的是同一个进程 —— 只认一种就漏掉一半存量配置。
# 局域网 IP 不在此列：那是另一台机器上的服务，改写它就是把能用的地形改成 404。
_LEGACY_APP_HOSTS = ("localhost", "127.0.0.1")


def normalize_parent_url(parent_url: str | None) -> str | None:
    """把 parentUrl 规整成**目录**形式，并把旧的应用内绝对地址改成相对路径；
    空值返回 None（= 不写该字段）。

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

    第二件事是把**旧的应用内**绝对地址 `http://localhost:5000/terrain/...` 改写
    成同名相对路径：瓦片自 0.3 起可能由 5001 专用 origin 提供，写死主端口会把
    父级请求绕回主连接池；远程访问时 `localhost` 更是指向客户端本机。相对地址
    由浏览器继承「提供这份 layer.json 的 origin」，两种情况都自动对。

    改写口径**故意很窄** —— 只认 http + 本机主端口（`localhost` 或 `127.0.0.1`
    + 5000）+ /terrain/ 前缀且不带 query/fragment。两个主机名一起收，是因为它们
    是同一个「应用自己的旧地址」的两种写法：启动横幅印的是 `localhost:5000`，
    而浏览器地址栏、手改 config 的人写 `127.0.0.1:5000` 同样常见，只修一种等于
    漏掉一半存量。外部域名、HTTPS、`localhost:5001`（瓦片专用 origin，不是要
    修的那个写死值）、局域网地址（那是别的机器上的服务）、带 query 的签名地址
    都保留原本的 scheme/host/port：那些是部署者明确配置的值，猜错方向就是把一
    个能用的地形服务改成 404。注意这只是
    「不换地址」，上面那段目录规整（尾部斜杠 + `/layer.json` 后缀）对所有值一
    视同仁 —— 那一条修的是 Cesium 的拼接行为，与地址指向哪里无关。
    """
    if not parent_url:
        return None
    url = parent_url.strip().rstrip("/")
    if url.lower().endswith("/layer.json"):
        url = url[: -len("/layer.json")].rstrip("/")
    if not url:
        return None

    try:
        parsed = urlsplit(url)
        is_legacy_app_url = (parsed.scheme.lower() == "http"
                             and parsed.hostname in _LEGACY_APP_HOSTS
                             and parsed.port == 5000
                             and parsed.path.startswith("/terrain/")
                             and not parsed.query
                             and not parsed.fragment)
    except ValueError:
        # 端口越界之类的畸形值：`.port` 会抛。原样保留 —— 这个函数的职责是规整，
        # 不是校验（写库那一侧有 config_manager._validate_browser_url）。
        return url
    if is_legacy_app_url:
        return parsed.path.rstrip("/")
    return url


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
    而默认值是应用内相对路径（`/terrain/base`），根本没有可请求的绝对地址 ——
    就算配的是完整 URL，网络探测也既慢又会引入不确定性；而 base 是本机磁盘上的
    产物，存在性是可靠的本地事实。
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
    指向本应用的 /terrain/base，目录拷到别的机器（那边没有 base、甚至没有本
    应用）上必然 404，而 Cesium 对这个 404 不
    报错，它塞一个假的 heightmap-1.0 图层并把 heightmapStructure 写在共享的
    builder 上，于是任务自己的 quantized-mesh 瓦片也按 heightmap 解析
    （v0.2.8 实测：4154 m 山峰解成 -744 m，控制台零报错）。

    maxzoom 取 max(底图, 任务, 声明层数-1)：maxzoom < 8 的退化任务里底图的 z6/z7
    比任务更深，写任务的值会把这两层声明掉，Cesium 从此不请求它们 —— 文件在目录
    里却看不到。第三项兜的是底图漏写 maxzoom 的情况。两个方向的代价不对称：报浅
    是硬闸门（整层直接消失），报深由 available 逐层兜住（空层 isTileAvailable
    返回 false，Cesium 不去请求，代价是零），所以安全边是往深了报。

    **available[i] 的绝对层号是 minzoom + i，不是 i。** 见 `cesiumlab_terrain.build_terrain`
    里 `for z in range(min_level, max_level + 1)` 逐层 append 出的
    `available_per_level`，以及它写进 layer.json 的 `"minzoom": min_level`。
    底图的原点是 0，任务的原点在底图
    可用时是 min(8, 有效 max_level)（tiler 恒传 8，build_terrain 再往下钳）——
    按下标对齐会把任务的 z8 声明并到底图的 z0 上，
    文件全在而声明全错，Cesium 拿着错声明去请求不存在的瓦片，又踩回上面那条
    404 → 假 heightmap → 共享 builder 污染的链。所以按绝对层号对齐，输出统一归一
    到 z0 为原点，minzoom 随之硬置 0（留着任务的 8 等于把刚植入的 z0-z7 全作废）。

    同层两边都有声明时取并集而不是二选一：磁盘上任务瓦片覆盖了底图的同名文件
    （植入是 skip-if-exists），但两边的 range 覆盖的 x/y 区间未必互相包含，丢
    掉任何一边都会漏声明真实存在的瓦片。range 去重只做逐字比较，不做区间归并
    —— Cesium 接受重叠声明，而重跑合成时的等值重复必须挡掉，否则声明会随重试
    次数线性膨胀。

    容忍 available / minzoom / maxzoom 缺失或为 null（底图允许用户自备），但不
    容忍 JSON 本身解不开：那时让异常抛给调用方回滚，静默吞掉只会产出一个没人知
    道是坏的 layer.json。
    """
    task = json.loads(task_layer_path.read_text(encoding="utf-8"))
    base = json.loads(base_layer_path.read_text(encoding="utf-8"))

    task_av = task.get("available") or []
    base_av = base.get("available") or []
    task_min = int(task.get("minzoom", 0) or 0)
    base_min = int(base.get("minzoom", 0) or 0)

    merged = []
    for z in range(max(base_min + len(base_av), task_min + len(task_av))):
        bi, ti = z - base_min, z - task_min
        ranges = list(base_av[bi] or []) if 0 <= bi < len(base_av) else []
        for r in ((task_av[ti] or []) if 0 <= ti < len(task_av) else []):
            if r not in ranges:
                ranges.append(r)
        merged.append(ranges)

    task["available"] = merged
    task["minzoom"] = 0
    task["maxzoom"] = max(int(base.get("maxzoom", 0) or 0),
                          int(task.get("maxzoom", 0) or 0),
                          len(merged) - 1)
    task.pop("parentUrl", None)

    task_layer_path.write_text(
        json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")