"""瓦片路径前缀白名单 —— **唯一一份**，三个消费者都从这里取。

这五条是「浏览地图」直接产生的高频请求：

  /basemap  底图转发(routes/basemap_static.py)
  /tiles    地图下载任务的瓦片(routes/tiles_static.py)
  /terrain  地形瓦片与全球底座(routes/terrain_static.py)
  /contour  等高线瓦片(routes/contour_static.py)
  /mbtiles  MBTiles 容器里的瓦片(routes/mbtiles_static.py)——影像/等高线/未来的
            MVT **共用这一条**路由，按 pipeline 分路而不是按数据类型各开一条。

消费者与漏改一处的后果：

- `src/core/tile_server.py`：瓦片端口放行谁，其余 404。漏加一条 = 那类瓦片在
  瓦片端口上得到硬 404。
- `src/core/logging_setup.py`：控制台丢掉谁的**成功**访问日志。漏加一条 =
  控制台被瓦片日志刷屏。
- `static/js/ui.js` 的 `TILE_PATH_PREFIXES`：浏览器把哪些路径改写到瓦片
  origin。跨语言抄不掉，由 tests/test_tile_server.py 的相等性断言钉住两边
  逐字一致 —— 前端多一条得跨源 404，少一条则那类瓦片仍挤在主端口的 6 条
  连接里。

注意结尾的 `/`：`/basemapx` 这类撞名路径不能混进来。

名单单独成模块、而不是放在 tile_server 里让 logging_setup 去 import：
logging_setup 在 app.py 里被刻意排在重量级 import(flask/GDAL)**之前**加载,
而 tile_server 顶部就 import flask 与 werkzeug —— 反过来依赖会把那几秒挪到
日志配置好之前,启动横幅之后的那段控制台又要重新变哑。本模块零依赖。
"""

# 判定一律用前缀而不是路由名：日志过滤器只拿得到一行文本、WSGI 包装层只拿得到
# PATH_INFO，两处都没有 Flask 的路由对象。
# 顺序即 static/js/ui.js 里那份镜像的顺序 —— 相等性断言逐字比较，新增一律追加
# 在末尾，不要为了「看起来整齐」重排（重排会让两侧在同一次提交里必须同步改）。
TILE_PATH_PREFIXES = ('/basemap/', '/tiles/', '/terrain/', '/contour/', '/mbtiles/')
